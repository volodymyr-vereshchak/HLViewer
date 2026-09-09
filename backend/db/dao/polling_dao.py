"""DAO for the GSM polling registry: agents, corrector cards, default hours.

Step 1 of docs/plans/gsm-polling.md — the settings side only. What an agent
asks for and reports back has its own endpoints and its own step.
"""
import hashlib
import secrets
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.device_catalog_model import CorectorType
from backend.db.models.dpd_line_model import DpdLine, DpdLineDevice
from backend.db.models.enterprise_model import (
    DpdDevice,
    Enterprise,
    EnterpriseDevice,
)
from backend.db.models.polling_model import (
    PollAgent,
    PollAgentDevice,
    PollDevice,
    PollSettings,
)

# The two kinds of corrector a card can point at, and the column for each.
# Kept in one place because the CHECK constraint, the API and the label lookup
# all have to agree on the same list.
TARGET_COLUMNS = ("dpd_device_id", "dpd_line_id")

TARGET_FIELDS = {
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

        `still_installed` is the quiet failure this screen exists to show. A
        card keeps dialling the corrector it names, so a replacement entered in
        Підприємства and not here means the modem is still calling a device
        that is no longer there.
        """
        rows = (await self.session.execute(
            select(
                PollDevice,
                DpdDevice.ser_num,
                DpdLine.name,
            )
            .outerjoin(DpdDevice, DpdDevice.id == PollDevice.dpd_device_id)
            .outerjoin(DpdLine, DpdLine.id == PollDevice.dpd_line_id)
            .order_by(PollDevice.id)
        )).all()

        assignments = await self.assignments()
        at_point = await self.points_of_devices()
        line_serials = await self.line_serials()
        result = []
        for card, ser_num, line_name in rows:
            if card.dpd_device_id is not None:
                kind = "dpd_device"
                point = at_point.get(card.dpd_device_id)
                label = point[0] if point else None
                still_installed = bool(point and point[1])
            else:
                kind = "dpd_line"
                label = line_name
                ser_num = line_serials.get(card.dpd_line_id)
                # A line's card follows the line, and the line's own history
                # says which corrector is on it — nothing to fall out of date.
                still_installed = ser_num is not None
            result.append({
                "card": card,
                "target_kind": kind,
                "target_label": label,
                "ser_num": ser_num,
                "still_installed": still_installed,
                "agent_ids": assignments.get(card.id, []),
            })
        return result

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

    async def line_serials(self) -> Dict[int, int]:
        """dpd line id -> serial of the corrector on it now.

        A DPD line keeps the corrector identity inline in its history rather
        than as a device row, so there is nothing to point a card at; the card
        points at the line and the serial is read from the current entry.
        """
        rows = (await self.session.execute(
            select(DpdLineDevice.dpd_line_id, DpdLineDevice.ser_num)
            .order_by(DpdLineDevice.installed_from)
        )).all()
        return {line_id: ser for line_id, ser in rows}

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
        for key, value in patch.items():
            setattr(card, key, value)
        await self.session.flush()
        return card

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

    async def set_poll_times(self, poll_times: List[str]) -> PollSettings:
        settings = await self.get_settings()
        # A new list, not an edit of the old one: SQLAlchemy tracks the
        # replacement, while mutating a JSONB value in place goes unnoticed.
        settings.poll_times = list(poll_times)
        await self.session.flush()
        return settings
