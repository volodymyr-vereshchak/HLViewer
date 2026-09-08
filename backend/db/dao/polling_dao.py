"""DAO for the GSM polling registry: agents, device cards, default hours.

Step 1 of docs/plans/gsm-polling.md — the settings side only. What an agent
asks for and reports back has its own endpoints and its own step.
"""
import hashlib
import secrets
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.enterprise_model import DpdDevice
from backend.db.models.dpd_line_model import DpdLine
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.polling_model import (
    PollAgent,
    PollAgentDevice,
    PollDevice,
    PollSettings,
)

# The three shapes a corrector takes in this database, and the column that
# points at each. Kept in one place because the CHECK constraint, the API and
# the label lookup all have to agree on the same list.
TARGET_COLUMNS = ("gas_volume_calc_id", "dpd_line_id", "dpd_device_id")


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

    # ── Device cards ─────────────────────────────────────────────────────────

    async def list_devices(self) -> List[Dict]:
        """Every card with the name of what it points at and who polls it.

        The label comes from a different table per target kind, so this is
        three outer joins rather than one: correctors are not one entity in
        this database, and pretending otherwise is what the CHECK constraint
        exists to prevent.
        """
        rows = (await self.session.execute(
            select(
                PollDevice,
                GasVolumeCalc.name,
                DpdLine.name,
                DpdDevice.ser_num,
            )
            .outerjoin(GasVolumeCalc,
                       GasVolumeCalc.id == PollDevice.gas_volume_calc_id)
            .outerjoin(DpdLine, DpdLine.id == PollDevice.dpd_line_id)
            .outerjoin(DpdDevice, DpdDevice.id == PollDevice.dpd_device_id)
            .order_by(PollDevice.id)
        )).all()

        assignments = await self.assignments()
        result = []
        for card, calc_name, line_name, ser_num in rows:
            if card.gas_volume_calc_id is not None:
                kind, label = "calc", calc_name
            elif card.dpd_line_id is not None:
                kind, label = "dpd_line", line_name
            else:
                kind, label = "dpd_device", (
                    f"№{ser_num}" if ser_num is not None else None
                )
            result.append({
                "card": card,
                "target_kind": kind,
                "target_label": label,
                "agent_ids": assignments.get(card.id, []),
            })
        return result

    async def assignments(self) -> Dict[int, List[int]]:
        """device id -> agents that took it. Empty list means nobody did, and
        a device nobody took is never polled — the one case the UI has to
        show loudly, because it looks exactly like "everything is fine"."""
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
        column = {
            "calc": PollDevice.gas_volume_calc_id,
            "dpd_line": PollDevice.dpd_line_id,
            "dpd_device": PollDevice.dpd_device_id,
        }[kind]
        return (await self.session.execute(
            select(PollDevice).where(column == target_id)
        )).scalars().first()

    async def create_device(self, data: Dict) -> PollDevice:
        card = PollDevice(**data)
        self.session.add(card)
        await self.session.flush()
        return card

    async def update_device(self, card: PollDevice, patch: Dict) -> PollDevice:
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
