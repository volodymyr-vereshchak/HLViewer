"""What an agent asks for and reports back.

Step 3 of docs/plans/gsm-polling.md. Seven calls, and not one of them is the
server waiting for anything: the modem is on somebody else's machine, so this
side only answers questions and records outcomes.

There are no poll jobs. An agent fetches a plan, works out nothing for itself —
the plan already says which devices are due — dials them, and reports. That is
why a workstation being switched off costs nothing: the device it did not reach
stays overdue in the data, and the next agent to ask sees exactly that.

Authenticated by `X-Agent-Key`, not by cookie: an agent is a service. These
paths are therefore exempt from the session middleware, and every one of them
resolves the key itself — an exemption from "must be signed in" would otherwise
be an exemption from any check at all.
"""
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.dao.dpd_line_dao import DpdLineArchiveDao
from backend.db.dao.polling_dao import PollingDao
from backend.db.engine import get_session
from backend.db.models.polling_model import PollAgent, PollDevice
from backend.services.poll_schedule import is_due

router = APIRouter(prefix="/polling/agent", tags=["polling-agent"])

# Path prefix the cookie middleware must let through; the key check below
# stands in its place.
AGENT_PATH_PREFIX = "/polling/agent"


async def current_agent(
    x_agent_key: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> PollAgent:
    agent = await PollingDao(session).agent_by_key(x_agent_key)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невідомий або вимкнений ключ агента",
        )
    return agent


# ── Schemas ───────────────────────────────────────────────────────────────────

class PlanDevice(BaseModel):
    """One device as the agent needs to see it: how to dial, what should
    answer, and whether it is due."""

    id: int
    due: bool
    due_reason: str
    priority: int

    # What must answer. A reply from any other serial is not written.
    ser_num: Optional[int] = None
    model_name: Optional[str] = None
    label: Optional[str] = None

    protocol_id: Optional[int] = None
    device_address: Optional[int] = None
    channel: str
    is_modem: bool
    phone: Optional[str] = None
    init_str: str
    dial_prefix: str
    tcp_host: Optional[str] = None
    tcp_port: Optional[int] = None
    answer_timeout_sec: int
    pause_between_ms: int
    repeat_count: int
    preamble_count: int
    modem_connect_timeout_sec: int
    modem_repeat_call_count: int
    modem_pause_after_connect_ms: int

    # Where to read from: the last stamp already stored, per archive. The
    # driver asks our stub for exactly this and reads forward from it, which
    # is what makes an incremental poll need no logic of its own.
    last_hour: Optional[datetime] = None
    last_day: Optional[datetime] = None

    last_poll_at: Optional[datetime] = None
    manual_requested_at: Optional[datetime] = None


class Plan(BaseModel):
    # The agent logs against this rather than its own clock; a workstation an
    # hour fast would otherwise misreport when things happened.
    server_time: datetime
    devices: List[PlanDevice]


class AgentDevices(BaseModel):
    device_ids: List[int]


class AgentState(BaseModel):
    """Telemetry, and the sign of life the admin screen reads."""

    version: Optional[str] = None
    host: Optional[str] = None
    polling_device_id: Optional[int] = None
    queue_length: int = 0


class LogLine(BaseModel):
    seq: int
    level: str = "info"
    message: str


class LogBatch(BaseModel):
    # The first batch of a session replaces what was there: this log answers
    # "what is happening now", not "what has ever happened".
    reset: bool = False
    lines: List[LogLine] = Field(default_factory=list)


class ArchiveRow(BaseModel):
    stamp: datetime
    volume: Optional[float] = None
    volume_work: Optional[float] = None
    pressure: Optional[float] = None
    temperature: Optional[float] = None
    press_unit: Optional[str] = None


class DataBatch(BaseModel):
    """One period type at a time, with the serial that actually answered."""

    ser_num: Optional[int] = None
    period_type: str  # hourly | daily
    rows: List[ArchiveRow] = Field(default_factory=list)


class Finish(BaseModel):
    status: str  # ok | error
    error_code: Optional[str] = None
    error_text: Optional[str] = None
    rows: Optional[Dict[str, int]] = None
    duration_ms: Optional[int] = None
    connect_ms: Optional[int] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _agent_card(
    dao: PollingDao, agent: PollAgent, device_id: int
) -> PollDevice:
    """The device, if this agent has it and is the one polling it.

    Checked on every call rather than once at `start`, because an agent that
    lost its claim — it hung for twenty minutes and somebody else took over —
    must stop writing. Otherwise two sessions would interleave into one log and
    one archive, and neither would be trustworthy.
    """
    card = await dao.get_device(device_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Прилад не знайдено")
    if device_id not in await dao.agent_devices(agent.id):
        raise HTTPException(status_code=403, detail="Прилад не закріплений за агентом")
    if card.polling_agent_id not in (None, agent.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Прилад зараз опитує інший агент",
        )
    return card


# ── The seven calls ───────────────────────────────────────────────────────────

@router.get("/plan", response_model=Plan)
async def get_plan(
    agent: PollAgent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    """Everything this agent needs to decide nothing.

    `due` is computed here rather than on the agent: workstation clocks drift,
    and a PC an hour fast would poll for nothing or miss its slot. Fetched
    every ten or fifteen seconds, which is also how long a change in the admin
    panel takes to reach the fleet — there is no scheduler to tell.
    """
    dao = PollingDao(session)
    now = datetime.now()
    mine = set(await dao.agent_devices(agent.id))
    settings = await dao.get_settings()
    coverage = await dao.archive_coverage([
        row["card"].id for row in await dao.list_devices()
    ])
    await dao.touch_agent(agent, None, None)
    await session.commit()

    devices = []
    for row in await dao.list_devices():
        card: PollDevice = row["card"]
        if card.id not in mine:
            continue
        due, reason = is_due(
            now=now,
            poll_times=card.poll_times,
            default_times=settings.poll_times,
            last_poll_at=card.last_poll_at,
            enabled=card.enabled,
            auto_poll=card.auto_poll,
            manual_requested_at=card.manual_requested_at,
        )
        last = coverage.get(card.id, {})
        devices.append(PlanDevice(
            id=card.id,
            due=due,
            due_reason=reason,
            priority=card.priority,
            ser_num=row["ser_num"],
            model_name=row["model_name"],
            label=row["target_label"],
            protocol_id=card.protocol_id,
            device_address=card.device_address,
            channel=card.channel,
            is_modem=card.is_modem,
            phone=card.phone,
            init_str=card.init_str,
            dial_prefix=card.dial_prefix,
            tcp_host=card.tcp_host,
            tcp_port=card.tcp_port,
            answer_timeout_sec=card.answer_timeout_sec,
            pause_between_ms=card.pause_between_ms,
            repeat_count=card.repeat_count,
            preamble_count=card.preamble_count,
            modem_connect_timeout_sec=card.modem_connect_timeout_sec,
            modem_repeat_call_count=card.modem_repeat_call_count,
            modem_pause_after_connect_ms=card.modem_pause_after_connect_ms,
            last_hour=last.get("hourly"),
            last_day=last.get("daily"),
            last_poll_at=card.last_poll_at,
            manual_requested_at=card.manual_requested_at,
        ))
    # Manual requests first, then priority, then oldest poll — the order an
    # agent would have had to work out for itself otherwise.
    devices.sort(key=lambda d: (
        d.manual_requested_at is None,
        d.priority,
        d.last_poll_at or datetime.min,
    ))
    return Plan(server_time=now, devices=devices)


@router.put("/devices", response_model=List[int])
async def set_my_devices(
    body: AgentDevices,
    agent: PollAgent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    """The set an operator ticked in the agent's own settings window.

    Kept on the server rather than locally, so the admin panel can show which
    devices nobody took — a device polled by no agent looks exactly like one
    that is working fine.
    """
    dao = PollingDao(session)
    await dao.set_agent_devices(agent.id, body.device_ids)
    await session.commit()
    return await dao.agent_devices(agent.id)


@router.post("/state")
async def report_state(
    body: AgentState,
    agent: PollAgent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    dao = PollingDao(session)
    await dao.touch_agent(agent, body.version, body.host)
    await session.commit()
    return {"ok": True}


@router.post("/devices/{device_id}/start")
async def start_session(
    device_id: int,
    agent: PollAgent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    """Take the device, or be told somebody else has it.

    409 is a normal answer, not a failure: two operators can tick the same
    device, and this is how they avoid dialling it at once. The agent skips to
    the next one.
    """
    dao = PollingDao(session)
    if device_id not in await dao.agent_devices(agent.id):
        raise HTTPException(status_code=403, detail="Прилад не закріплений за агентом")
    if not await dao.claim(device_id, agent.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Прилад зараз опитує інший агент",
        )
    await session.commit()
    return {"ok": True}


@router.post("/devices/{device_id}/log")
async def push_log(
    device_id: int,
    body: LogBatch,
    agent: PollAgent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    dao = PollingDao(session)
    await _agent_card(dao, agent, device_id)
    if body.reset:
        await dao.clear_log(device_id)
    highest = await dao.append_log(
        device_id, [line.model_dump() for line in body.lines]
    )
    await session.commit()
    return {"last_seq": highest}


@router.post("/devices/{device_id}/data")
async def push_data(
    device_id: int,
    body: DataBatch,
    agent: PollAgent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    """Store what the device answered — if it is the device we asked for.

    The serial check is the reason a card names a corrector at all. A reply
    from another device is not written and not silently accepted: it means
    either a replacement nobody recorded or a call that reached the wrong
    place, and writing one meter's archive under another's name is worse than
    a missing reading.

    Re-polling a period is safe: the archive's unique constraint turns it into
    an update of the same values.
    """
    if body.period_type not in ("hourly", "daily"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Очікується hourly або daily",
        )
    dao = PollingDao(session)
    card = await _agent_card(dao, agent, device_id)

    expected = next(
        (r["ser_num"] for r in await dao.list_devices() if r["card"].id == device_id),
        None,
    )
    if body.ser_num is not None and expected is not None and body.ser_num != expected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Відповів прилад №{body.ser_num}, а очікувався №{expected}. "
                f"Дані не збережено"
            ),
        )

    if not body.rows:
        return {"stored": 0}

    if card.dpd_device_id is not None:
        await DpdArchiveDao(session).upsert_records(
            body.period_type,
            [
                {
                    "device_id": card.dpd_device_id,
                    "stamp": r.stamp,
                    "dvst_alwrk": r.volume,
                    "dvwrk_alwrk": r.volume_work,
                    "press": r.pressure,
                    "temper": r.temperature,
                    "press_unit": r.press_unit,
                }
                for r in body.rows
            ],
            source="gsm",
        )
    else:
        await DpdLineArchiveDao(session).upsert_records(
            body.period_type,
            [
                {
                    "dpd_line_id": card.dpd_line_id,
                    "stamp": r.stamp,
                    "volume": r.volume,
                    "pressure": r.pressure,
                    "temperature": r.temperature,
                    "press_unit": r.press_unit,
                }
                for r in body.rows
            ],
        )
    await session.commit()
    return {"stored": len(body.rows)}


@router.post("/devices/{device_id}/finish")
async def finish_session(
    device_id: int,
    body: Finish,
    agent: PollAgent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    """Close the session and release the device.

    `last_poll_at` moves only on success. A failed attempt has to leave the
    device overdue, so the next agent tries again instead of treating the
    failure as a job done.
    """
    dao = PollingDao(session)
    card = await _agent_card(dao, agent, device_id)
    await dao.finish(
        card,
        agent.id,
        status="ok" if body.status == "ok" else "error",
        error_code=body.error_code,
        error_text=body.error_text,
        rows=body.rows,
        duration_ms=body.duration_ms,
        connect_ms=body.connect_ms,
    )
    await session.commit()
    return {"ok": True}
