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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.dao.dpd_line_dao import DpdLineArchiveDao
from backend.db.dao.polling_dao import PollingDao
from backend.db.engine import get_session
from backend.db.models.polling_model import PollAgent, PollDevice
from backend.services import agent_version, poll_journal
from backend.services.poll_schedule import is_due
from backend.settings import backend_settings

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
    #: Which line of the corrector the point is metered on, as ДПД numbers
    #: them. Only a multi-line corrector cares, and only the agent's Універсал
    #: reader asks for it by name.
    ch_num: Optional[int] = None
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

    #: A Floutek ТМ-2 asks for it in every archive request.
    device_password: Optional[str] = None


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


class Progress(BaseModel):
    """How far the session has got, in records."""

    done: int = 0
    total: int = 0
    #: Which ring those numbers count: "hourly" or "daily". A bar labelled
    #: from the first phase it saw spent the whole daily read calling days
    #: hours.
    phase: Optional[str] = None


class LogBatch(BaseModel):
    # The first batch of a session replaces what was there: this log answers
    # "what is happening now", not "what has ever happened".
    reset: bool = False
    lines: List[LogLine] = Field(default_factory=list)
    # Sent as the agent reads, so the screen can show a bar rather than a
    # spinner that never moves.
    progress: Optional[Progress] = None


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
    #: What answered, as the agent found out on the call: the protocol it
    #: spoke and the model the corrector named. Absent when nothing did.
    protocol_id: Optional[int] = None
    model: Optional[str] = None


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
    # A day covers every schedule in use; anything older cannot belong to the
    # slot being counted.
    failures = await dao.failed_attempts_since(now - timedelta(days=1))
    await dao.touch_agent(agent, None, None)
    await session.commit()

    # An agent that is not the build this server hands out is told so once, in
    # the plan, instead of being refused device by device: it stops dialling
    # the moment it asks, and the reason travels with every card it can see.
    wanted = agent_version.expected()
    outdated = not agent_version.matches(agent.version, wanted)

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
            last_attempt_at=card.last_attempt_at,
            last_status=card.last_status,
            scheduled_failures=failures.get(card.id, []),
        )
        # An enterprise card whose corrector has been taken away has nothing
        # to dial for. Saying so here keeps the agent from making a call that
        # would look, from the far end, exactly like a meter that will not
        # answer.
        if not row["still_installed"] and card.enterprise_id is not None:
            due, reason = False, "немає встановлених корректорів"

        # And a site somebody else is already dialling is not due for this
        # agent, however overdue it is. The same site is often given to two
        # machines so that one being switched off does not stop it; calling it
        # due for both made the loser ask for the plan again the moment it was
        # refused, as fast as the network allowed, for the length of the other
        # call. The claim, not the schedule, is what settles this.
        if due and card.polling_agent_id != agent.id and dao.claim_is_live(card, now):
            due, reason = False, "опитує інший агент"

        if outdated:
            due, reason = False, f"агент {agent.version or '—'}, потрібен {wanted}"

        last = coverage.get(card.id, {})
        devices.append(PlanDevice(
            id=card.id,
            due=due,
            due_reason=reason,
            priority=card.priority,
            ser_num=row["ser_num"],
            ch_num=row.get("ch_num"),
            model_name=row["model_name"],
            label=row["target_label"],
            # What answered last time, else the catalogue's guess. Only an
            # order of questions: the agent asks every family it reads.
            protocol_id=card.detected_protocol or row["protocol_id"],
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
            # Sent for every card: which corrector answers is found on the
            # call, and only a ТМ-2 uses it.
            device_password=card.device_password,
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
    # Before anything else: an agent that is not the build this server hands
    # out does not poll. See services/agent_version — the build of 14.09 wrote
    # a whole column of pressures a factor of ten out, and the workstation
    # carried on running it after the fix had shipped.
    wanted = agent_version.expected()
    if not agent_version.matches(agent.version, wanted):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=agent_version.refusal(agent.version, wanted),
        )
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
    # This is also a heartbeat, and the only one there is during a call.
    # `/state` is sent between sessions, so an agent reading a ВЕГА — a hundred
    # seconds for a day of hours, and an hour for a first fill — went quiet for
    # longer than AGENT_SILENCE and showed as offline on the very screen that
    # was watching it work. The progress of that read arrives here every few
    # seconds anyway; nothing extra has to be sent.
    await dao.touch_agent(agent, None, None)
    card = await _agent_card(dao, agent, device_id)
    lines = [line.model_dump() for line in body.lines]
    path = _journal_of(card)
    if body.reset:
        card.progress_done = None
        card.progress_total = None
        card.progress_phase = None
        # One file per session: the screen that follows the call reads this
        # same file, so the previous session must not still be in it.
        poll_journal.start(path, await _journal_title(dao, card, agent))
    if body.progress is not None:
        card.progress_done = body.progress.done
        card.progress_total = body.progress.total
        card.progress_phase = body.progress.phase
    poll_journal.append(path, lines)
    await session.commit()
    # The answer carries the one instruction this protocol has. The agent
    # talks to us every couple of seconds while it reads and at no other
    # time, so a cancel asked for from the browser reaches the modem in about
    # that long — without inventing a channel for it.
    return {
        "last_seq": max((line.get("seq", 0) for line in lines), default=0),
        "cancel": card.cancel_requested_at is not None,
    }


def _journal_of(card: PollDevice) -> Path:
    return poll_journal.journal_path(
        backend_settings["POLL_LOG_DIR"], card.enterprise_id, card.id
    )


async def _journal_title(dao: PollingDao, card: PollDevice,
                         agent: PollAgent) -> str:
    """The line a file opens with: who was dialled, by which machine."""
    name = None
    if card.enterprise_id is not None:
        name = (await dao.enterprise_names()).get(card.enterprise_id)
    return (f"Опитування: {name or f'картка {card.id}'}"
            f"{f', {card.phone}' if card.phone else ''}, агент «{agent.name}»")


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

    # Where the readings go. An enterprise card names no corrector, so the
    # archive is the one of whatever is fitted there now — the same resolution
    # the plan used to decide what to dial. Without this the rows fell through
    # to the line archive and were refused by a NOT NULL, after the call had
    # already been made.
    # Named apart from `device_id`, which is the CARD in this handler: the
    # two are different things and one of them is in the URL.
    archive_device_id = card.dpd_device_id
    if archive_device_id is None and card.enterprise_id is not None:
        fitted = (await dao.correctors_of_enterprises()).get(card.enterprise_id)
        if fitted is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Немає встановлених корректорів — дані не збережено",
            )
        archive_device_id = fitted["device_id"]

    if archive_device_id is not None:
        await DpdArchiveDao(session).upsert_records(
            body.period_type,
            [
                {
                    "device_id": archive_device_id,
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
    # Remembered only when something answered: a call that reached nobody
    # says nothing about what is on the other end, and must not wipe what an
    # earlier call learned.
    if body.protocol_id is not None:
        card.detected_protocol = body.protocol_id
    if body.model:
        card.detected_model = body.model[:64]
    # The file ends with the answer, so it does not have to be inferred from
    # the last thing that happened to be logged.
    poll_journal.finish(
        _journal_of(card), body.status, body.error_text, body.rows,
        body.duration_ms,
    )
    await session.commit()
    return {"ok": True}
