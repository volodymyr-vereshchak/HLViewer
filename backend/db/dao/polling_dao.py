"""DAO for the GSM polling registry: agents, corrector cards, default hours.

Step 1 of docs/plans/gsm-polling.md — the settings side only. What an agent
asks for and reports back has its own endpoints and its own step.
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.device_catalog_model import CorectorType, Manufacturer
from backend.db.models.dpd_line_model import DpdLine, DpdLineDevice
from backend.db.models.enterprise_model import (
    DpdDevice,
    Enterprise,
    EnterpriseDevice,
)
from backend.db.models.polling_model import (
    PollAgent,
    PollAgentDevice,
    PollAttempt,
    PollDevice,
    PollSettings,
)
from backend.services.poll_validation import (
    DEFAULT_DEVICE_ADDRESS, address_matters, normalise_phone,
)

# What a card can point at, and the column for each. Kept in one place
# because the CHECK constraint, the API and the label lookup all have to agree
# on the same list.
TARGET_COLUMNS = ("enterprise_id", "dpd_device_id", "dpd_line_id")

TARGET_FIELDS = {
    "enterprise": PollDevice.enterprise_id,
    "dpd_device": PollDevice.dpd_device_id,
    "dpd_line": PollDevice.dpd_line_id,
}


def generate_agent_key() -> str:
    """A key for one agent machine. Shown once, then only its hash is kept."""
    return secrets.token_urlsafe(32)


def hash_agent_key(key: str) -> str:
    """SHA-256, deliberately not bcrypt.

    bcrypt is slow on purpose, which is right for a human password someone
    might guess. An agent key is 32 random bytes — guessing is not the threat —
    and the agent presents it on every plan, log and data call of every poll.
    A per-request bcrypt verify would cost more than the poll it authorises.
    """
    return hashlib.sha256(key.encode()).hexdigest()


class PollingDao:
    """Reads and writes the polling registry. Commits are the caller's."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ── Corrector cards ──────────────────────────────────────────────────────

    async def list_devices(self) -> List[Dict]:
        """Every card with the corrector it dials and where that corrector
        stands.

        The serial is the point of the card: it is what the modem expects to
        hear back, and a reply from anything else is refused. For an enterprise
        corrector it comes from the device row; for a DPD line the history
        keeps the serial inline, so the current entry is read instead.

        The model comes with it, because a serial alone does not say what is on
        the other end of the line — and the model is what decides the driver,
        whether the network address is a question, and how the alarm codes are
        read.

        `still_installed` is the quiet failure this screen exists to show. A
        card keeps dialling the corrector it names, so a replacement entered in
        Підприємства and not here means the modem is still calling a device
        that is no longer there.
        """
        rows = (await self.session.execute(
            select(
                PollDevice,
                DpdDevice.ser_num,
                DpdDevice.ch_num,
                CorectorType.model_name,
                Manufacturer.short_name,
                DpdLine.name,
            )
            .outerjoin(DpdDevice, DpdDevice.id == PollDevice.dpd_device_id)
            .outerjoin(CorectorType,
                       CorectorType.id == DpdDevice.corector_type_id)
            .outerjoin(Manufacturer,
                       Manufacturer.id == CorectorType.manufacturer_id)
            .outerjoin(DpdLine, DpdLine.id == PollDevice.dpd_line_id)
            .order_by(PollDevice.id)
        )).all()

        assignments = await self.assignments()
        at_point = await self.points_of_devices()
        line_devices = await self.line_devices()
        fitted = await self.correctors_of_enterprises()
        names = await self.enterprise_names()
        result = []
        for card, ser_num, ch_num, model_name, mfr_name, line_name in rows:
            protocol_id = None
            if card.enterprise_id is not None:
                kind = "enterprise"
                label = names.get(card.enterprise_id)
                # Resolved now, not stored: which corrector answers this
                # phone is a fact about today, not a setting.
                current = fitted.get(card.enterprise_id)
                ser_num = current["ser_num"] if current else None
                ch_num = current["ch_num"] if current else None
                model_name = current["model_name"] if current else None
                mfr_name = current["manufacturer"] if current else None
                still_installed = current is not None
                protocol_id = current["protocol_id"] if current else None
            elif card.dpd_device_id is not None:
                kind = "dpd_device"
                point = at_point.get(card.dpd_device_id)
                label = point[0] if point else None
                still_installed = bool(point and point[1])
            else:
                kind = "dpd_line"
                label = line_name
                # A line's card follows the line, and the line's own history
                # says which corrector is on it — nothing to fall out of date.
                current = line_devices.get(card.dpd_line_id)
                ser_num = current[0] if current else None
                model_name = current[1] if current else None
                mfr_name = current[2] if current else None
                ch_num = current[3] if current else None
                still_installed = ser_num is not None
            result.append({
                "card": card,
                "target_kind": kind,
                # For an enterprise card the driver is a property of the
                # corrector fitted today, not of the card.
                "protocol_id": (
                    protocol_id if kind == "enterprise" else card.protocol_id
                ),
                "target_label": label,
                "ser_num": ser_num,
                "ch_num": ch_num,
                "model_name": model_name,
                "manufacturer": mfr_name,
                "still_installed": still_installed,
                "agent_ids": assignments.get(card.id, []),
            })
        return result

    #: How long an agent may be silent before it is no longer a free modem.
    #: Three plan fetches at the usual interval: one missed is a hiccup, three
    #: is a workstation that has gone home.
    AGENT_SILENCE = timedelta(seconds=60)

    async def agents_for(self, card_id: int) -> List[PollAgent]:
        """Every active agent this card was given to, awake or not."""
        return list((await self.session.execute(
            select(PollAgent)
            .join(PollAgentDevice, PollAgentDevice.agent_id == PollAgent.id)
            .where(PollAgentDevice.poll_device_id == card_id)
            .where(PollAgent.active.is_(True))
        )).scalars().all())

    async def free_agents_for(self, card_id: int) -> List[PollAgent]:
        """Agents that took this card and are alive enough to answer.

        "Immediate" is only true if somebody is listening. An agent whose
        machine was switched off still holds its assignment, and a request
        handed to it would sit unread until morning — which the screen would
        show as a poll that simply never finished.

        Kept apart from `agents_for` because the difference is the whole of
        the message an operator gets: nobody assigned is a card to set up,
        and everybody asleep is a machine to switch on. Both used to read
        "немає вільного модема", which is advice for neither.
        """
        alive = datetime.now() - self.AGENT_SILENCE
        return [a for a in await self.agents_for(card_id)
                if a.last_seen_at and a.last_seen_at >= alive]

    async def gsm_of_enterprise(self, enterprise_id: int) -> Optional[PollDevice]:
        return (await self.session.execute(
            select(PollDevice).where(PollDevice.enterprise_id == enterprise_id)
        )).scalars().first()

    async def gsm_by_enterprise(self) -> Dict[int, PollDevice]:
        """Every enterprise that has a modem, in one query.

        The list screen needs this for the whole fleet at once — asking per
        row would be a query per enterprise, and the industry list runs to
        hundreds.
        """
        rows = (await self.session.execute(
            select(PollDevice).where(PollDevice.enterprise_id.isnot(None))
        )).scalars().all()
        return {card.enterprise_id: card for card in rows}

    async def gsm_of_line(self, dpd_line_id: int) -> Optional[PollDevice]:
        return (await self.session.execute(
            select(PollDevice).where(PollDevice.dpd_line_id == dpd_line_id)
        )).scalars().first()

    async def gsm_by_line(self) -> Dict[int, PollDevice]:
        """Every ДПД line that has a modem, in one query — as for enterprises."""
        rows = (await self.session.execute(
            select(PollDevice).where(PollDevice.dpd_line_id.isnot(None))
        )).scalars().all()
        return {card.dpd_line_id: card for card in rows}

    async def set_gsm(self, enterprise_id: int, phone: Optional[str],
                      auto_poll: bool, poll_cron: Optional[str],
                      password: Optional[str] = None) -> None:
        """Create, update or remove the modem settings of one enterprise."""
        await self._set_gsm(await self.gsm_of_enterprise(enterprise_id),
                            {"enterprise_id": enterprise_id},
                            phone, auto_poll, poll_cron, password)

    async def set_line_gsm(self, dpd_line_id: int, phone: Optional[str],
                           auto_poll: bool, poll_cron: Optional[str],
                           password: Optional[str] = None) -> None:
        """The same, for a ДПД line: those have modems of their own.

        A line's card differs from an enterprise's in one thing only — which
        corrector answers. At a site it is whichever one is fitted today; on a
        line it is the one the line's own history names.
        """
        await self._set_gsm(await self.gsm_of_line(dpd_line_id),
                            {"dpd_line_id": dpd_line_id},
                            phone, auto_poll, poll_cron, password)

    async def _set_gsm(self, card: Optional[PollDevice], owner: Dict,
                       phone: Optional[str], auto_poll: bool,
                       poll_cron: Optional[str],
                       password: Optional[str]) -> None:
        """Create, update or remove one modem card.

        A phone cleared to empty removes the card altogether rather than
        leaving one that can never dial: a card with no number is a scheduled
        poll that fails every night for a reason nobody can see from the list.
        """
        phone = normalise_phone(phone)

        if not phone:
            if card is not None:
                await self.session.delete(card)
            return

        values = {
            "phone": phone,
            "auto_poll": auto_poll,
            "poll_cron": poll_cron or None,
            "enabled": True,
            "device_password": (password or "").strip() or "11",
        }
        if card is None:
            self.session.add(PollDevice(**owner, **values))
        else:
            for field, value in values.items():
                setattr(card, field, value)
        await self.session.flush()

    async def correctors_of_enterprises(self) -> Dict[int, Dict]:
        """enterprise id -> the corrector standing there right now.

        This is what makes an enterprise-bound card work: the modem is at the
        site and stays, the corrector under it gets replaced, and nobody has
        to remember to repoint anything. `removed_at IS NULL` is "still
        fitted"; where a point somehow has two, the later installation wins.

        An enterprise with nothing fitted is absent from the result, and the
        poll refuses rather than dialling a site whose corrector was taken
        away — a call to a modem with no meter behind it looks exactly like a
        meter that will not answer.
        """
        rows = (await self.session.execute(
            select(
                EnterpriseDevice.enterprise_id,
                DpdDevice.id,
                DpdDevice.ser_num,
                DpdDevice.ch_num,
                CorectorType.model_name,
                CorectorType.protocol_id,
                Manufacturer.short_name,
            )
            .join(DpdDevice, DpdDevice.id == EnterpriseDevice.device_id)
            .outerjoin(CorectorType,
                       CorectorType.id == DpdDevice.corector_type_id)
            .outerjoin(Manufacturer,
                       Manufacturer.id == CorectorType.manufacturer_id)
            .where(EnterpriseDevice.removed_at.is_(None))
            .order_by(EnterpriseDevice.installed_from)
        )).all()
        return {
            enterprise_id: {
                "device_id": device_id,
                "ser_num": ser_num,
                # Which line of the corrector this point is metered on. A
                # Універсал carries two and answers for the one it is asked
                # about: at МАС СІДС one stands idle while the other has the
                # gas, and a poll of the wrong one reads zeros all day.
                "ch_num": ch_num,
                "model_name": model_name,
                "protocol_id": protocol_id,
                "manufacturer": mfr,
            }
            for enterprise_id, device_id, ser_num, ch_num, model_name, protocol_id, mfr
            in rows
        }

    async def enterprise_names(self) -> Dict[int, str]:
        return dict((await self.session.execute(
            select(Enterprise.id, Enterprise.enterprise_name)
        )).all())

    async def points_of_devices(self) -> Dict[int, tuple]:
        """device id -> (point name, is it still fitted there).

        A corrector taken off a point keeps its history entry, with
        `removed_at` set; the card that names it is then dialling a device that
        is gone, which is exactly what the screen has to say out loud.
        """
        rows = (await self.session.execute(
            select(
                EnterpriseDevice.device_id,
                Enterprise.enterprise_name,
                EnterpriseDevice.removed_at,
                EnterpriseDevice.installed_from,
            )
            .join(Enterprise, Enterprise.id == EnterpriseDevice.enterprise_id)
            .order_by(EnterpriseDevice.installed_from)
        )).all()
        # Ordered by install moment, so the latest assignment of each device
        # wins — a corrector moved between points belongs to the last one.
        return {
            device_id: (name, removed_at is None)
            for device_id, name, removed_at, _ in rows
        }

    async def line_devices(self) -> Dict[int, tuple]:
        """dpd line id -> (serial, model, manufacturer, channel) of its corrector.

        A DPD line keeps the corrector identity inline in its history rather
        than as a device row, so there is nothing to point a card at; the card
        points at the line and the current entry says what is on it.
        """
        rows = (await self.session.execute(
            select(
                DpdLineDevice.dpd_line_id,
                DpdLineDevice.ser_num,
                CorectorType.model_name,
                Manufacturer.short_name,
                # Which line of the corrector this ДПД line is metered on: a
                # Універсал carries two and answers for the one it is asked
                # about.
                DpdLineDevice.ch_num,
            )
            .outerjoin(CorectorType,
                       CorectorType.id == DpdLineDevice.corector_type_id)
            .outerjoin(Manufacturer,
                       Manufacturer.id == CorectorType.manufacturer_id)
            .order_by(DpdLineDevice.installed_from)
        )).all()
        # Ordered by install moment, so the last write per line wins.
        return {line_id: (ser, model, mfr, ch)
                for line_id, ser, model, mfr, ch in rows}

    async def assignments(self) -> Dict[int, List[int]]:
        """card id -> agents that took it. Empty list means nobody did, and a
        corrector nobody took is never polled — the one case the UI has to show
        loudly, because it looks exactly like "everything is fine"."""
        rows = (await self.session.execute(
            select(PollAgentDevice.poll_device_id, PollAgentDevice.agent_id)
        )).all()
        out: Dict[int, List[int]] = {}
        for device_id, agent_id in rows:
            out.setdefault(device_id, []).append(agent_id)
        return out

    async def get_device(self, device_id: int) -> Optional[PollDevice]:
        return await self.session.get(PollDevice, device_id)

    async def find_by_target(self, kind: str, target_id: int) -> Optional[PollDevice]:
        return (await self.session.execute(
            select(PollDevice).where(TARGET_FIELDS[kind] == target_id)
        )).scalars().first()

    async def protocol_of_device(self, device_id: int) -> Optional[int]:
        """The driver that can speak to this corrector, from its model.

        Per model, not per device: it is a property of the make, and asking an
        operator to retype it on every card invites a typo that looks exactly
        like a dead meter. NULL is a real answer for a good part of this fleet
        — ТКБ, smart104 and ТАНДЕМ appear in none of the Ask2 drivers.
        """
        return (await self.session.execute(
            select(CorectorType.protocol_id)
            .join(DpdDevice, DpdDevice.corector_type_id == CorectorType.id)
            .where(DpdDevice.id == device_id)
        )).scalars().first()

    async def create_device(self, data: Dict) -> PollDevice:
        if data.get("protocol_id") is None and data.get("dpd_device_id"):
            data["protocol_id"] = await self.protocol_of_device(
                data["dpd_device_id"]
            )
        data["device_address"] = self._address_for(
            data.get("protocol_id"), data.get("device_address")
        )
        card = PollDevice(**data)
        self.session.add(card)
        await self.session.flush()
        return card

    async def update_device(self, card: PollDevice, patch: Dict) -> PollDevice:
        # Repointing at another corrector re-reads the driver: a replacement is
        # often a different model, and a card left on the old driver would dial
        # the new device in a language it does not speak.
        if "dpd_device_id" in patch and patch["dpd_device_id"]:
            patch["protocol_id"] = await self.protocol_of_device(
                patch["dpd_device_id"]
            )
            # The new model may not be a Floutek, in which case whatever
            # address the old one needed is no longer a choice.
            patch["device_address"] = self._address_for(
                patch["protocol_id"], patch.get("device_address")
            )
        elif "device_address" in patch:
            patch["device_address"] = self._address_for(
                card.protocol_id, patch["device_address"]
            )
        for key, value in patch.items():
            setattr(card, key, value)
        await self.session.flush()
        return card

    @staticmethod
    def _address_for(protocol_id: Optional[int], typed: Optional[int]) -> int:
        """The network address to store.

        Only Floutek gives an operator a real choice — several correctors share
        one line there. Every other driver still sends the address and checks
        it in the reply, but with one device per line it is always the default,
        so it is filled in rather than asked for: a typo in a field nobody
        needed to touch looks exactly like a dead meter.
        """
        if address_matters(protocol_id) and typed is not None:
            return typed
        return DEFAULT_DEVICE_ADDRESS

    async def delete_device(self, card: PollDevice) -> None:
        await self.session.delete(card)

    async def request_manual(self, card: PollDevice, user_id: Optional[int]) -> None:
        """Ask for an out-of-turn poll. The agent picks it up with its next
        plan; there is nothing to start from here, because the modem is on
        somebody else's machine."""
        card.manual_requested_at = datetime.now()
        card.manual_requested_by = user_id

    async def cancel_manual(self, card: PollDevice) -> None:
        card.manual_requested_at = None
        card.manual_requested_by = None

    # ── Agents ───────────────────────────────────────────────────────────────

    async def list_agents(self) -> List[Dict]:
        counts = dict((await self.session.execute(
            select(PollAgentDevice.agent_id, func.count())
            .group_by(PollAgentDevice.agent_id)
        )).all())
        agents = (await self.session.execute(
            select(PollAgent).order_by(PollAgent.name)
        )).scalars().all()
        return [{"agent": a, "device_count": counts.get(a.id, 0)} for a in agents]

    async def busy_agents(self) -> Dict[int, Dict]:
        """What each agent is on the line with right now, by the claim it holds.

        The claim is the only record of a call in progress the server has —
        the modem is on somebody else's machine — and it is exactly what the
        agents screen was missing: an agent that is online says nothing about
        whether it is dialling or idle.
        """
        fresh = datetime.now() - self.CLAIM_TIMEOUT
        rows = (await self.session.execute(
            select(PollDevice, DpdLine.name)
            .outerjoin(DpdLine, DpdLine.id == PollDevice.dpd_line_id)
            .where(PollDevice.polling_agent_id.isnot(None))
            .where(PollDevice.polling_since.isnot(None))
            .where(PollDevice.polling_since >= fresh)
            .order_by(PollDevice.polling_since)
        )).all()
        if not rows:
            return {}
        names = await self.enterprise_names()
        at_point = await self.points_of_devices()
        busy: Dict[int, Dict] = {}
        for card, line_name in rows:
            if card.enterprise_id is not None:
                label = names.get(card.enterprise_id)
            elif card.dpd_line_id is not None:
                label = line_name
            else:
                point = at_point.get(card.dpd_device_id)
                label = point[0] if point else None
            # One modem, one call: if a stale claim ever left two, the newest
            # is the one being dialled now.
            busy[card.polling_agent_id] = {
                "poll_device_id": card.id,
                "label": label,
                "since": card.polling_since,
                "phase": card.progress_phase,
                "done": card.progress_done,
                "total": card.progress_total,
            }
        return busy

    async def get_agent(self, agent_id: int) -> Optional[PollAgent]:
        return await self.session.get(PollAgent, agent_id)

    async def create_agent(self, data: Dict) -> tuple[PollAgent, str]:
        """Returns the agent and its key IN CLEAR — the only time it exists.

        Losing it means issuing a new one, which is the intended trade: a key
        that can be read back later is a key that leaks from the screen it is
        read on.
        """
        key = generate_agent_key()
        agent = PollAgent(**data, key_hash=hash_agent_key(key))
        self.session.add(agent)
        await self.session.flush()
        return agent, key

    async def rotate_agent_key(self, agent: PollAgent) -> str:
        key = generate_agent_key()
        agent.key_hash = hash_agent_key(key)
        await self.session.flush()
        return key

    async def delete_agent(self, agent: PollAgent) -> None:
        await self.session.delete(agent)

    async def agent_devices(self, agent_id: int) -> List[int]:
        return list((await self.session.execute(
            select(PollAgentDevice.poll_device_id)
            .where(PollAgentDevice.agent_id == agent_id)
        )).scalars())

    async def set_agent_devices(self, agent_id: int, device_ids: List[int]) -> None:
        """Replace the agent's set. Operators split the fleet between
        themselves by ticking boxes, so this is a whole-set write: a diff
        would need the UI to know what it started from, and two operators
        editing at once would then silently undo each other."""
        await self.session.execute(
            delete(PollAgentDevice).where(PollAgentDevice.agent_id == agent_id)
        )
        for device_id in dict.fromkeys(device_ids):
            self.session.add(
                PollAgentDevice(agent_id=agent_id, poll_device_id=device_id)
            )
        await self.session.flush()

    async def device_agents(self, device_id: int) -> List[int]:
        """Agents that took one card — the single-card form of `assignments`."""
        return list((await self.session.execute(
            select(PollAgentDevice.agent_id)
            .where(PollAgentDevice.poll_device_id == device_id)
            .order_by(PollAgentDevice.agent_id)
        )).scalars().all())

    async def set_device_agents(self, device_id: int, agent_ids: List[int]) -> None:
        """Replace the machines that poll this one enterprise.

        The mirror of set_agent_devices, and it exists because the monitor
        screen is a list of enterprises, not of agents: asked from that side,
        "who dials this site" is one row's question, and answering it through
        the agent-shaped call would mean rewriting every agent's whole set to
        move one site between two of them.
        """
        await self.session.execute(
            delete(PollAgentDevice).where(
                PollAgentDevice.poll_device_id == device_id
            )
        )
        for agent_id in dict.fromkeys(agent_ids):
            self.session.add(
                PollAgentDevice(agent_id=agent_id, poll_device_id=device_id)
            )
        await self.session.flush()

    async def stored_stamps(
        self, device_id: int, start: datetime, end: datetime
    ) -> tuple[list, list]:
        """What the archive already holds for one corrector in one window.

        Returned as bare stamps rather than counted: the caller is looking for
        holes, and a count only answers "how many", never "which".
        """
        hours = list((await self.session.execute(
            text("""
                SELECT stamp FROM dpd_hourly_archive
                 WHERE device_id = :device AND stamp BETWEEN :start AND :end
            """),
            {"device": device_id, "start": start, "end": end},
        )).scalars())
        days = list((await self.session.execute(
            text("""
                SELECT day FROM dpd_daily_archive
                 WHERE device_id = :device AND day BETWEEN :start AND :end
            """),
            {"device": device_id, "start": start.date(), "end": end.date()},
        )).scalars())
        return hours, days

    async def archive_device_of(self, card: PollDevice) -> Optional[int]:
        """The corrector whose archive this card's readings belong to.

        For an enterprise card that is whatever is fitted there now — the same
        resolution the plan uses to decide what to dial, so a poll and its
        storage can never disagree about which meter they are talking about.
        """
        if card.dpd_device_id is not None:
            return card.dpd_device_id
        if card.enterprise_id is None:
            return None
        fitted = (await self.correctors_of_enterprises()).get(card.enterprise_id)
        return fitted["device_id"] if fitted else None

    async def failed_attempts_since(
        self, since: datetime
    ) -> Dict[int, List[datetime]]:
        """When each card's recent SCHEDULED attempts failed, newest first.

        Calls somebody asked for by hand are left out on purpose: they are a
        person checking, not the schedule trying, and using them up would mean
        that looking at a site removes the automatic retries it still had.

        The schedule counts these per slot: three calls to a line that is not
        answering is as much as a retry can fix, and the fourth is a phone bill
        with a modem the rest of the fleet cannot use meanwhile.

        One query for the whole fleet — the plan is fetched every fifteen
        seconds by every agent, so a query per card would be the expensive part
        of the day.
        """
        rows = (await self.session.execute(
            select(PollAttempt.poll_device_id, PollAttempt.started_at)
            .where(PollAttempt.started_at >= since)
            .where(PollAttempt.status != "ok")
            .where(PollAttempt.manual.is_(False))
            .order_by(PollAttempt.started_at.desc())
        )).all()
        out: Dict[int, List[datetime]] = {}
        for card_id, started_at in rows:
            out.setdefault(card_id, []).append(started_at)
        return out

    async def archive_coverage(self, card_ids: List[int]) -> Dict[int, Dict]:
        """card id -> {"hourly": last stamp, "daily": last day} already stored.

        This is where an incremental poll comes from, and there is deliberately
        no coverage table to keep in step: the answer is MAX() over the archive
        itself. DPD needed one because it backfills into the PAST through a
        metered API; a modem only ever reads forward from the last record, so a
        separate ledger could only drift from what is actually stored.
        """
        if not card_ids:
            return {}
        rows = (await self.session.execute(
            text(
                """
                SELECT p.id,
                       (SELECT MAX(h.stamp) FROM dpd_hourly_archive h
                         WHERE h.device_id = COALESCE(p.dpd_device_id, fitted.device_id))
                                                             AS dev_hour,
                       (SELECT MAX(d.day) FROM dpd_daily_archive d
                         WHERE d.device_id = COALESCE(p.dpd_device_id, fitted.device_id))
                                                             AS dev_day,
                       (SELECT MAX(lh.stamp) FROM dpd_line_hourly_archive lh
                         WHERE lh.dpd_line_id = p.dpd_line_id) AS line_hour,
                       (SELECT MAX(ld.day) FROM dpd_line_daily_archive ld
                         WHERE ld.dpd_line_id = p.dpd_line_id) AS line_day
                FROM poll_device p
                -- An enterprise card's coverage is the coverage of whatever
                -- corrector stands there now. A replacement therefore starts
                -- from the new serial's own history rather than re-reading
                -- everything the old one had already delivered.
                LEFT JOIN LATERAL (
                    SELECT ed.device_id
                      FROM enterprise_device ed
                     WHERE ed.enterprise_id = p.enterprise_id
                       AND ed.removed_at IS NULL
                     ORDER BY ed.installed_from DESC
                     LIMIT 1
                ) AS fitted ON TRUE
                WHERE p.id = ANY(:ids)
                """
            ),
            {"ids": card_ids},
        )).all()
        out: Dict[int, Dict] = {}
        for card_id, dev_hour, dev_day, line_hour, line_day in rows:
            day = dev_day or line_day
            out[card_id] = {
                "hourly": dev_hour or line_hour,
                # The daily archive keys by date; the agent wants a moment.
                "daily": datetime.combine(day, datetime.min.time()) if day else None,
            }
        return out

    # ── What an agent does ───────────────────────────────────────────────────

    async def agent_by_key(self, key: str) -> Optional[PollAgent]:
        """The agent presenting this key, if it is active.

        Only the hash is stored, so this is a lookup by hash rather than a
        comparison — there is nothing to compare against.
        """
        return (await self.session.execute(
            select(PollAgent)
            .where(PollAgent.key_hash == hash_agent_key(key))
            .where(PollAgent.active.is_(True))
        )).scalars().first()

    async def touch_agent(
        self, agent: PollAgent, version: Optional[str], host: Optional[str]
    ) -> None:
        """Heartbeat plus self-description. `last_seen_at` is how the admin
        screen decides an agent has gone quiet — there is nothing else to ask."""
        agent.last_seen_at = datetime.now()
        if version:
            agent.version = version[:32]
        if host:
            agent.host = host[:255]

    #: How long a claim survives without being finished. Long enough for a
    #: month of hours down a phone line; short enough that a machine switched
    #: off mid-session frees the site the same working day.
    CLAIM_TIMEOUT = timedelta(minutes=20)

    def claim_is_live(self, card: PollDevice, now: datetime) -> bool:
        """Is somebody dialling this right now."""
        return (card.polling_agent_id is not None
                and card.polling_since is not None
                and now - card.polling_since < self.CLAIM_TIMEOUT)

    async def claim(self, device_id: int, agent_id: int) -> bool:
        """Take a device for the length of one session.

        Needed for exactly one case: a device two operators both ticked, whose
        agents both decide at the same moment that it is overdue. One atomic
        UPDATE settles it — nothing returned means somebody else is already
        dialling, and this agent moves on.

        A claim releases itself after twenty minutes, which is what makes an
        agent switched off mid-session cost nothing: no cleanup, no lease
        renewal, no way for a crashed machine to hold a device forever.
        """
        # The time comes from here, not from the database's now(). Postgres
        # runs in the container's UTC and the application in Europe/Kyiv, so a
        # claim stamped by the database and read back by Python looked three
        # hours old the moment it was made — mutual exclusion still worked
        # (both ends of that comparison were the database's clock), but every
        # other reading of polling_since was wrong by the offset.
        now = datetime.now()
        result = await self.session.execute(
            text(
                "UPDATE poll_device "
                "SET polling_agent_id = :agent, polling_since = :now "
                "WHERE id = :device "
                "  AND (polling_since IS NULL OR polling_since < :cutoff) "
                "RETURNING id"
            ),
            {"agent": agent_id, "device": device_id,
             "now": now, "cutoff": now - self.CLAIM_TIMEOUT},
        )
        return result.first() is not None

    async def release(self, card: PollDevice) -> None:
        card.polling_agent_id = None
        card.polling_since = None
        # A cancellation belongs to the call it stopped. Left standing, it
        # would end the next one before it had begun — and the next one is
        # usually the retry somebody asked for after cancelling.
        card.cancel_requested_at = None
        card.progress_phase = None

    async def finish(
        self,
        card: PollDevice,
        agent_id: int,
        *,
        status: str,
        error_code: Optional[str],
        error_text: Optional[str],
        rows: Optional[Dict],
        duration_ms: Optional[int],
        connect_ms: Optional[int],
    ) -> None:
        """Record the outcome of one session.

        `last_poll_at` moves only on success, and it is the only thing the
        schedule reads: a failed attempt must leave the device overdue, so the
        next agent tries again rather than treating the failure as done.
        """
        now = datetime.now()
        card.last_attempt_at = now
        card.last_status = status
        card.last_error_code = error_code
        card.last_error_text = error_text
        card.last_agent_id = agent_id
        card.last_rows = rows or {}
        card.last_duration_ms = duration_ms
        card.last_connect_ms = connect_ms
        if status == "ok":
            card.last_poll_at = now
        # Whatever the outcome: the button was pressed once, and the session
        # it asked for has happened. Clearing it only on success left a failed
        # manual poll due for ever — the agent redialled every fifteen seconds,
        # three attempts at a time, until somebody noticed the phone bill. A
        # failure is reported on the screen; whether to try again is a decision
        # for the person who pressed the button, and the schedule keeps its own
        # retry through `last_poll_at`, which still does not move.
        # Recorded before it is cleared: a call somebody asked for is not one
        # the schedule made, and the retry budget is the schedule's.
        was_manual = card.manual_requested_at is not None
        card.manual_requested_at = None
        card.manual_requested_by = None
        self.session.add(PollAttempt(
            poll_device_id=card.id,
            manual=was_manual,
            agent_id=agent_id,
            started_at=now,
            finished_at=now,
            status=status,
            error_code=error_code,
            rows=rows or {},
        ))
        await self.release(card)

    # ── Default hours ────────────────────────────────────────────────────────

    async def get_settings(self) -> PollSettings:
        settings = await self.session.get(PollSettings, 1)
        if settings is None:
            # Tests build the schema from metadata and get no seeded row; the
            # migration seeds one in production. Either way the answer to "what
            # are the default hours" must not be an exception.
            settings = PollSettings(id=1)
            self.session.add(settings)
            await self.session.flush()
        return settings

    async def set_poll_cron(self, poll_cron: str) -> PollSettings:
        settings = await self.get_settings()
        # A new list, not an edit of the old one: SQLAlchemy tracks the
        # replacement, while mutating a JSONB value in place goes unnoticed.
        settings.poll_cron = poll_cron
        await self.session.flush()
        return settings
