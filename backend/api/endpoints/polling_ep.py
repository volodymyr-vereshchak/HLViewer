"""Settings for the GSM poll: device cards, agents, default hours.

Step 1 of docs/plans/gsm-polling.md. Nothing here polls anything — these are
the screens an administrator sets the fleet up on. What an agent asks for and
reports back lives on its own routes, authenticated by key rather than cookie,
and arrives with step 3.

Who may do what: settings are administrative, but **asking for an
out-of-turn poll is not** — that was a deliberate decision, because the person
who notices a device has gone quiet is rarely the person with the admin role.
The write is admitted by name in the auth middleware; every other write here
stays admin-only by default.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_current_user, require_admin
from backend.db.dao.polling_dao import PollingDao
from backend.db.engine import get_session
from backend.db.models.app_user_model import AppUser
from backend.db.models.polling_model import PollDevice
from backend.services import poll_journal
from backend.services.agent_installer import find_installer
from backend.services import agent_version
from backend.services.poll_validation import (
    DEFAULT_DEVICE_ADDRESS,
    PollValidationError,
    address_matters,
    normalise_phone,
    validate_poll_times,
    validate_priority,
)
from backend.settings import backend_settings

router = APIRouter(
    prefix="/polling",
    tags=["polling"],
    dependencies=[Depends(get_current_user)],
)

TARGET_KINDS = ("dpd_device", "dpd_line")


# ── Schemas ───────────────────────────────────────────────────────────────────

class PollDeviceLink(BaseModel):
    """Everything about reaching the device. A copy of ask2cfg.xml, so that
    moving the settings across is mechanical rather than selective."""

    channel: str = "com"
    is_modem: bool = True
    phone: Optional[str] = None
    init_str: str = "AT&F"
    dial_prefix: str = "ATDP"
    tcp_host: Optional[str] = None
    tcp_port: Optional[int] = None

    device_address: Optional[int] = None
    answer_timeout_sec: int = 7
    pause_between_ms: int = 400
    repeat_count: int = 3
    preamble_count: int = 0
    priority: int = 0
    note: Optional[str] = None

    modem_connect_timeout_sec: int = 40
    modem_repeat_call_count: int = 3
    modem_pause_after_connect_ms: int = 400

    is_adapter: bool = False
    adapter_line: Optional[int] = None
    adapter_speed: Optional[int] = None
    adapter_level_in: Optional[int] = None
    adapter_level_out: Optional[int] = None
    adapter_is_frequency: bool = False
    adapter_is_radio: bool = False

    enabled: bool = True
    auto_poll: bool = True
    poll_times: Optional[List[str]] = None


class PollDeviceCreate(PollDeviceLink):
    #: The usual one: the modem stands at the enterprise and the corrector
    #: under it is whichever is fitted today.
    enterprise_id: Optional[int] = None
    dpd_device_id: Optional[int] = None
    dpd_line_id: Optional[int] = None

    @model_validator(mode="after")
    def exactly_one_target(self):
        targets = [self.enterprise_id, self.dpd_device_id, self.dpd_line_id]
        if sum(target is not None for target in targets) != 1:
            # The database says the same thing, but an IntegrityError here
            # would reach the operator as "500" instead of a sentence.
            raise ValueError(
                "Вкажіть рівно одну ціль: підприємство, коректор або лінію ДПД"
            )
        return self


class PollDeviceUpdate(BaseModel):
    """Every field optional: an omitted one keeps its value."""

    model_config = {"extra": "forbid"}

    channel: Optional[str] = None
    is_modem: Optional[bool] = None
    phone: Optional[str] = None
    init_str: Optional[str] = None
    dial_prefix: Optional[str] = None
    tcp_host: Optional[str] = None
    tcp_port: Optional[int] = None
    device_address: Optional[int] = None
    answer_timeout_sec: Optional[int] = None
    pause_between_ms: Optional[int] = None
    repeat_count: Optional[int] = None
    preamble_count: Optional[int] = None
    priority: Optional[int] = None
    note: Optional[str] = None
    modem_connect_timeout_sec: Optional[int] = None
    modem_repeat_call_count: Optional[int] = None
    modem_pause_after_connect_ms: Optional[int] = None
    is_adapter: Optional[bool] = None
    adapter_line: Optional[int] = None
    adapter_speed: Optional[int] = None
    adapter_level_in: Optional[int] = None
    adapter_level_out: Optional[int] = None
    adapter_is_frequency: Optional[bool] = None
    adapter_is_radio: Optional[bool] = None
    enabled: Optional[bool] = None
    auto_poll: Optional[bool] = None
    poll_times: Optional[List[str]] = None
    # Repointing a corrector-bound card at another serial IS how a
    # replacement was recorded here. Enterprise-bound cards need none of it:
    # the corrector is resolved at poll time from the installation history.
    dpd_device_id: Optional[int] = None


class PollDeviceRead(PollDeviceLink):
    id: int
    dpd_device_id: Optional[int] = None
    dpd_line_id: Optional[int] = None
    target_kind: str
    enterprise_id: Optional[int] = None
    # Where the corrector stands: the metering point, or the DPD line.
    target_label: Optional[str] = None
    # What the modem expects to hear back. A reply from any other serial is
    # refused, so this is the whole point of the card.
    ser_num: Optional[int] = None
    # A serial alone does not say what is on the other end of the line, and the
    # model is what decides the driver, whether the address is a question, and
    # how the alarm codes are read.
    model_name: Optional[str] = None
    manufacturer: Optional[str] = None
    # False when the corrector this card names is no longer fitted — a
    # replacement entered in Підприємства and not here.
    still_installed: bool = True
    # Taken from the corrector's model, never typed here. None means no Ask2
    # driver covers that model, which for part of this fleet is the truth.
    protocol_id: Optional[int] = None
    # Whether the network address is a real choice for this driver — only for
    # Floutek, where several correctors share a line. Everywhere else it stays
    # at its default and the form has no business asking.
    address_matters: bool = False
    # Which agents took this device. Empty means nobody polls it at all.
    agent_ids: List[int] = Field(default_factory=list)

    last_poll_at: Optional[datetime] = None
    last_attempt_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error_code: Optional[str] = None
    last_error_text: Optional[str] = None
    last_agent_id: Optional[int] = None
    last_rows: dict = Field(default_factory=dict)
    last_duration_ms: Optional[int] = None
    last_connect_ms: Optional[int] = None
    manual_requested_at: Optional[datetime] = None
    polling_agent_id: Optional[int] = None
    polling_since: Optional[datetime] = None


class PollAgentCreate(BaseModel):
    name: str
    branch_id: Optional[int] = None
    active: bool = True


class PollAgentUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    name: Optional[str] = None
    branch_id: Optional[int] = None
    active: Optional[bool] = None


class PollAgentRead(BaseModel):
    id: int
    name: str
    branch_id: Optional[int] = None
    active: bool
    last_seen_at: Optional[datetime] = None
    version: Optional[str] = None
    host: Optional[str] = None
    device_count: int = 0
    #: Decided here rather than in the browser, and by the same rule that
    #: refuses an immediate poll with "немає вільного модема". Two rules for
    #: one fact would eventually disagree, and the screen would be calling an
    #: agent online while the poll it offers is refused.
    online: bool = False
    #: Whether this agent is the build the server hands out. A mismatch stops
    #: its polls, so the screen has to say it rather than leave an operator
    #: watching a machine that never picks anything up.
    version_ok: bool = True
    expected_version: Optional[str] = None


class PollAgentCreated(PollAgentRead):
    """The one response that carries the key in clear."""

    key: str


class AgentDevices(BaseModel):
    device_ids: List[int]


class ScheduleRead(BaseModel):
    poll_times: List[str]


def _read(row: dict) -> PollDeviceRead:
    card: PollDevice = row["card"]
    # The driver is the fitted corrector's, not the card's, so an enterprise
    # card that has had three correctors reports whichever one is there now.
    protocol_id = row["protocol_id"]
    return PollDeviceRead(
        **card.model_dump(exclude={"created_at", "updated_at", "protocol_id"}),
        protocol_id=protocol_id,
        target_kind=row["target_kind"],
        target_label=row["target_label"],
        ser_num=row["ser_num"],
        model_name=row["model_name"],
        manufacturer=row["manufacturer"],
        still_installed=row["still_installed"],
        address_matters=address_matters(protocol_id),
        agent_ids=row["agent_ids"],
    )


async def _reread(dao: PollingDao, device_id: int) -> PollDeviceRead:
    """The response comes from the list query, not from the ORM object.

    The target label and the agents that took the device are joins, and a card
    that answered without them would be missing exactly the two fields the
    screen is built around.
    """
    rows = await dao.list_devices()
    return _read(next(r for r in rows if r["card"].id == device_id))


def _clean(payload: dict) -> dict:
    """Normalise what the operator typed, or answer with a sentence.

    Everything here fails on somebody else's machine hours later if it gets
    through — an undialable number, a slot the agent never reaches — so it is
    refused at the door rather than stored.
    """
    try:
        if "phone" in payload:
            payload["phone"] = normalise_phone(payload["phone"])
        if "poll_times" in payload:
            payload["poll_times"] = validate_poll_times(payload["poll_times"])
        if "priority" in payload:
            payload["priority"] = validate_priority(payload["priority"])
    except PollValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    return payload


# ── Device cards ──────────────────────────────────────────────────────────────

@router.get("/devices", response_model=List[PollDeviceRead])
async def list_devices(session: AsyncSession = Depends(get_session)):
    """Readable by anyone signed in: which devices are polled, by whom, and
    when each last answered is an operational question, not an admin one."""
    return [_read(row) for row in await PollingDao(session).list_devices()]


@router.post(
    "/devices",
    response_model=PollDeviceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_device(
    body: PollDeviceCreate, session: AsyncSession = Depends(get_session)
):
    dao = PollingDao(session)
    try:
        card = await dao.create_device(_clean(body.model_dump()))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Для цього коректора вже є картка опитування",
        )
    return await _reread(dao, card.id)


@router.get(
    "/devices/{device_id}",
    response_model=PollDeviceRead,
    dependencies=[Depends(require_admin)],
)
async def get_device(device_id: int, session: AsyncSession = Depends(get_session)):
    """Admin-only, unlike the list: this is the full card, phone number
    and timings included."""
    rows = await PollingDao(session).list_devices()
    row = next((r for r in rows if r["card"].id == device_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Картку опитування не знайдено")
    return _read(row)


@router.put(
    "/devices/{device_id}",
    response_model=PollDeviceRead,
    dependencies=[Depends(require_admin)],
)
async def update_device(
    device_id: int,
    body: PollDeviceUpdate,
    session: AsyncSession = Depends(get_session),
):
    dao = PollingDao(session)
    card = await dao.get_device(device_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Картку опитування не знайдено")
    patch = _clean(body.model_dump(exclude_unset=True))
    if "dpd_device_id" in patch and card.dpd_line_id is not None:
        # A line's card follows the line; its corrector comes from the line's
        # own history and there is nothing here to repoint.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Картка лінії ДПД не прив'язується до коректора вручну",
        )
    try:
        # update_device flushes, so the unique index fires here rather than at
        # commit — both have to be inside the same guard.
        await dao.update_device(card, patch)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Для цього коректора вже є картка опитування",
        )
    return await _reread(dao, device_id)


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_device(device_id: int, session: AsyncSession = Depends(get_session)):
    dao = PollingDao(session)
    card = await dao.get_device(device_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Картку опитування не знайдено")
    await dao.delete_device(card)
    await session.commit()


@router.post("/devices/{device_id}/poll", status_code=status.HTTP_202_ACCEPTED)
async def request_poll(
    device_id: int,
    cancel: bool = False,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(get_current_user),
):
    """Ask for an out-of-turn poll — allowed to any signed-in user.

    202, not 200: nothing starts here. The modem is on somebody else's
    machine, and the request is picked up when that agent next asks for its
    plan. Telling the caller otherwise would be a lie the UI then has to
    explain away.
    """
    dao = PollingDao(session)
    card = await dao.get_device(device_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Картку опитування не знайдено")
    if cancel:
        await dao.cancel_manual(card)
    else:
        await dao.request_manual(card, user.id)
    await session.commit()
    return {"requested_at": card.manual_requested_at}


class EnterprisePollStart(BaseModel):
    poll_device_id: int
    ser_num: Optional[int] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None


@router.post(
    "/enterprises/{enterprise_id}/poll",
    response_model=EnterprisePollStart,
    status_code=status.HTTP_202_ACCEPTED,
)
async def poll_enterprise(
    enterprise_id: int,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(get_current_user),
):
    """Poll this enterprise over its modem, now.

    Every refusal here is one the operator would otherwise meet as a call that
    goes nowhere, several minutes later:

      * no modem set up — the enterprise card has no number;
      * nothing fitted — the correctors were all taken off, so there is
        nothing behind that phone to read;
      * no free modem — the agents that took this card are all switched off,
        and the request would sit unread until somebody came back.

    202 rather than 200: the modem is on somebody else's machine. What starts
    here is the request; the agent picks it up within seconds and the screen
    follows it through the session log.
    """
    dao = PollingDao(session)
    card = await dao.gsm_of_enterprise(enterprise_id)
    if card is None:
        raise HTTPException(
            status_code=422,
            detail="Для цього підприємства не налаштовано модем",
        )

    fitted = (await dao.correctors_of_enterprises()).get(enterprise_id)
    if fitted is None:
        raise HTTPException(
            status_code=422, detail="Немає встановлених корректорів",
        )
    if fitted["protocol_id"] is None:
        raise HTTPException(
            status_code=422,
            detail=f"Модель «{fitted['model_name'] or '—'}» модемом не опитується",
        )

    # Three different problems used to share one sentence, and none of them
    # is "немає вільного модема" in the sense an operator reads it.
    assigned = await dao.agents_for(card.id)
    if not assigned:
        raise HTTPException(
            status_code=422,
            detail=(
                "Цьому підприємству не призначено жодного агента. "
                "Призначте машину з модемом у картці підприємства, "
                "блок «Опитування модемом»."
            ),
        )
    agents = await dao.free_agents_for(card.id)
    if not agents:
        names = ", ".join(a.name for a in assigned)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Агент {names} не на зв'язку — програма на тій машині не "
                f"запущена. Запит нікому передати."
                if len(assigned) == 1 else
                f"Жоден із агентів ({names}) не на зв'язку — програми на тих "
                f"машинах не запущені. Запит нікому передати."
            ),
        )

    await dao.request_manual(card, user.id)
    # The previous session's journal is deliberately left alone: it is the
    # only account of the last call, and a request that no agent picks up
    # would otherwise destroy it for nothing. The watch endpoint knows not to
    # show it as this call's — see `read_last_log` / `watch_enterprise_poll`.
    await session.commit()
    return EnterprisePollStart(
        poll_device_id=card.id,
        ser_num=fitted["ser_num"],
        model_name=fitted["model_name"],
        agent_name=agents[0].name,
    )


class PollCancelled(BaseModel):
    """What stopping a poll actually did."""

    #: "queued" — the request was withdrawn before any agent took it;
    #: "asked" — a call is in progress and the agent has been told to hang up;
    #: "idle" — there was nothing running to stop.
    outcome: str
    detail: str


@router.post(
    "/enterprises/{enterprise_id}/poll/cancel",
    response_model=PollCancelled,
)
async def cancel_enterprise_poll(
    enterprise_id: int,
    session: AsyncSession = Depends(get_session),
    _: AppUser = Depends(get_current_user),
):
    """Stop a poll that was started by mistake.

    Two quite different things wear the same button.

    A request no agent has taken is simply withdrawn — nothing has happened
    yet, and nothing is lost.

    A call already in progress cannot be stopped from here at all: the modem
    is on somebody else's machine. What this does is raise a flag that the
    agent reads on its next log push, a second or two later, and hangs up
    **between records** — never in the middle of one.

    What has been read so far is then thrown away, and that is the careful
    choice rather than the lazy one. Coverage is measured from the newest
    record stored, so keeping three hours of an interrupted eleven would move
    it past the eight underneath and nobody would ever ask for them again.
    The readings are not lost — they are still in the corrector, and the next
    poll reads them. A cancelled call costs the minutes it ran, not any data.
    """
    dao = PollingDao(session)
    card = await dao.gsm_of_enterprise(enterprise_id)
    if card is None:
        raise HTTPException(
            status_code=404, detail="Для цього підприємства не налаштовано модем"
        )

    if card.polling_agent_id is not None:
        card.cancel_requested_at = datetime.now()
        await session.commit()
        return PollCancelled(
            outcome="asked",
            detail="Агенту передано зупинитись — він покладе слухавку за кілька секунд",
        )

    if card.manual_requested_at is not None:
        await dao.cancel_manual(card)
        await session.commit()
        return PollCancelled(
            outcome="queued", detail="Запит знято — агент його не встиг узяти",
        )

    return PollCancelled(outcome="idle", detail="Зараз нічого не опитується")


class PollLogLine(BaseModel):
    seq: int
    #: None for a line the journal could not date — one written by an older
    #: version, or by the corrector itself. Shown without a time rather than
    #: dropped.
    ts: Optional[datetime] = None
    level: str
    message: str


class PollWatch(BaseModel):
    """What the screen shows while a poll runs."""

    poll_device_id: int
    status: str                       # waiting | polling | ok | error
    agent_name: Optional[str] = None
    ser_num: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_text: Optional[str] = None
    rows: dict = Field(default_factory=dict)
    done: Optional[int] = None
    total: Optional[int] = None
    #: Which archive `done`/`total` are counting: "hourly" or "daily".
    phase: Optional[str] = None
    #: Somebody asked this call to stop and the agent has not hung up yet.
    cancelling: bool = False
    lines: List[PollLogLine] = Field(default_factory=list)


@router.get("/enterprises/{enterprise_id}/poll", response_model=PollWatch)
async def watch_enterprise_poll(
    enterprise_id: int,
    after_seq: int = 0,
    session: AsyncSession = Depends(get_session),
    _: AppUser = Depends(get_current_user),
):
    """Follow a poll as it happens, one screen-refresh at a time.

    `after_seq` is what makes this cheap to call every second: the browser
    already has the lines it has, and asks only for what came after them.

    The status is derived rather than stored. A card with a holder is being
    polled; one with a pending request and no holder is waiting for its agent
    to notice; anything else is the outcome of the last session — which is
    what an operator who walked away and came back needs to see.
    """
    dao = PollingDao(session)
    card = await dao.gsm_of_enterprise(enterprise_id)
    if card is None:
        raise HTTPException(
            status_code=404, detail="Для цього підприємства не налаштовано модем"
        )

    if card.polling_agent_id is not None:
        state = "polling"
    elif card.manual_requested_at is not None:
        state = "waiting"
    elif card.last_status == "ok":
        state = "ok"
    elif card.last_status:
        state = "error"
    else:
        state = "waiting"

    agents = {row["agent"].id: row["agent"].name for row in await dao.list_agents()}
    fitted = (await dao.correctors_of_enterprises()).get(enterprise_id)
    return PollWatch(
        poll_device_id=card.id,
        status=state,
        agent_name=agents.get(card.polling_agent_id or card.last_agent_id),
        ser_num=fitted["ser_num"] if fitted else None,
        started_at=card.polling_since or card.manual_requested_at,
        finished_at=card.last_attempt_at,
        error_code=card.last_error_code,
        error_text=card.last_error_text,
        rows=card.last_rows or {},
        done=card.progress_done,
        total=card.progress_total,
        phase=card.progress_phase,
        cancelling=card.cancel_requested_at is not None,
        lines=[PollLogLine(**line) for line in _session_lines(card, after_seq)],
    )


def _session_lines(card: PollDevice, after_seq: int) -> List[dict]:
    """The journal of the session the screen is watching — and only that one.

    The file holds the last call, which is usually the one being asked about.
    The exception is a request nobody has picked up yet: the file is then the
    *previous* call, and showing it would read as this one already running.
    """
    waiting = (card.polling_agent_id is None
               and card.manual_requested_at is not None
               and (card.last_attempt_at is None
                    or card.last_attempt_at < card.manual_requested_at))
    if waiting:
        return []

    path = poll_journal.journal_path(
        backend_settings["POLL_LOG_DIR"], card.enterprise_id, card.id
    )
    return poll_journal.read_lines(path, after_seq)


class DeviceAgents(BaseModel):
    agent_ids: List[int]


@router.put(
    "/devices/{device_id}/agents",
    response_model=List[int],
    dependencies=[Depends(require_admin)],
)
async def set_device_agents(
    device_id: int,
    body: DeviceAgents,
    session: AsyncSession = Depends(get_session),
):
    """Which machines dial this enterprise.

    Asked from the monitor, where a row is a site rather than an agent. A site
    nobody took is never polled, and it looks exactly like a site that is
    working — which is why the screen shows the column at all.
    """
    dao = PollingDao(session)
    if await dao.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Картку опитування не знайдено")
    await dao.set_device_agents(device_id, body.agent_ids)
    await session.commit()
    return (await dao.assignments()).get(device_id, [])


class PollJournal(BaseModel):
    """The log of the last poll of one site, as it was written down."""

    poll_device_id: int
    #: None when this site has never been polled from here.
    text: Optional[str] = None
    updated_at: Optional[datetime] = None


@router.get("/devices/{device_id}/log/last", response_model=PollJournal)
async def read_last_log(
    device_id: int,
    session: AsyncSession = Depends(get_session),
):
    """What the last session did — after the live log has been wiped.

    Not admin-only, for the same reason asking for a poll is not: the person
    who notices a site has gone quiet is rarely the person with the admin
    role, and this is the first thing they need to look at.
    """
    card = await PollingDao(session).get_device(device_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Картку опитування не знайдено")

    path = poll_journal.journal_path(
        backend_settings["POLL_LOG_DIR"], card.enterprise_id, card.id
    )
    text = poll_journal.read(path)
    return PollJournal(
        poll_device_id=device_id,
        text=text,
        updated_at=(datetime.fromtimestamp(path.stat().st_mtime)
                    if text is not None else None),
    )


# ── Agents ────────────────────────────────────────────────────────────────────

@router.get(
    "/agents",
    response_model=List[PollAgentRead],
    dependencies=[Depends(require_admin)],
)
async def list_agents(session: AsyncSession = Depends(get_session)):
    alive = datetime.now() - PollingDao.AGENT_SILENCE
    wanted = agent_version.expected()
    return [
        PollAgentRead(
            **row["agent"].model_dump(),
            device_count=row["device_count"],
            online=bool(row["agent"].last_seen_at
                        and row["agent"].last_seen_at >= alive),
            version_ok=agent_version.matches(row["agent"].version, wanted),
            expected_version=wanted,
        )
        for row in await PollingDao(session).list_agents()
    ]


class AgentInstaller(BaseModel):
    """What the admin panel needs to offer the download, or explain its absence."""

    available: bool
    filename: Optional[str] = None
    version: Optional[str] = None
    size: Optional[int] = None
    built_at: Optional[datetime] = None


@router.get(
    "/agents/installer/info",
    response_model=AgentInstaller,
    dependencies=[Depends(require_admin)],
)
async def agent_installer_info():
    """Is there a build to download, and which one.

    Asked before the button is drawn: an enabled button that answers 404 is
    worse than a line saying the server has no build yet, because the second
    tells whoever reads it what to do about it.
    """
    found = find_installer(backend_settings["AGENT_DIST_DIR"])
    if found is None:
        return AgentInstaller(available=False)
    return AgentInstaller(
        available=True,
        filename=found.filename,
        version=found.version,
        size=found.size,
        built_at=found.built_at,
    )


#: What each build is served as. A wrong type is not fatal — the browser saves
#: the file either way — but it is what decides whether it opens a "save" dialog
#: or tries to display the bytes.
INSTALLER_TYPES = {
    ".zip": "application/zip",
    ".exe": "application/vnd.microsoft.portable-executable",
}


@router.get("/agents/installer", dependencies=[Depends(require_admin)])
async def download_agent_installer():
    """The build itself.

    Admin-only, like the rest of this screen — not because the file is a
    secret (it is useless without a key) but because the machine that polls
    meters is set up by the person who also issues the key, and offering the
    download anywhere else invites copies nobody assigned.
    """
    found = find_installer(backend_settings["AGENT_DIST_DIR"])
    if found is None:
        raise HTTPException(
            status_code=404,
            detail="Збірку агента не завантажено на сервер",
        )
    return FileResponse(
        found.path,
        media_type=INSTALLER_TYPES.get(found.path.suffix.lower(),
                                       "application/octet-stream"),
        filename=found.filename,
    )


@router.post(
    "/agents",
    response_model=PollAgentCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_agent(
    body: PollAgentCreate, session: AsyncSession = Depends(get_session)
):
    """The response carries the key in clear, and it is the only time it
    exists anywhere. Lost means reissued — a key that can be read back later
    is a key that leaks from wherever it is read."""
    dao = PollingDao(session)
    try:
        agent, key = await dao.create_agent(body.model_dump())
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Агент з такою назвою вже існує",
        )
    return PollAgentCreated(**agent.model_dump(), device_count=0, key=key)


@router.put(
    "/agents/{agent_id}",
    response_model=PollAgentRead,
    dependencies=[Depends(require_admin)],
)
async def update_agent(
    agent_id: int,
    body: PollAgentUpdate,
    session: AsyncSession = Depends(get_session),
):
    dao = PollingDao(session)
    agent = await dao.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Агента не знайдено")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(agent, key, value)
    await session.commit()
    return PollAgentRead(
        **agent.model_dump(),
        device_count=len(await dao.agent_devices(agent_id)),
    )


@router.post(
    "/agents/{agent_id}/key",
    response_model=PollAgentCreated,
    dependencies=[Depends(require_admin)],
)
async def rotate_key(agent_id: int, session: AsyncSession = Depends(get_session)):
    """Issue a new key; the old one stops working immediately."""
    dao = PollingDao(session)
    agent = await dao.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Агента не знайдено")
    key = await dao.rotate_agent_key(agent)
    await session.commit()
    return PollAgentCreated(
        **agent.model_dump(),
        device_count=len(await dao.agent_devices(agent_id)),
        key=key,
    )


@router.delete(
    "/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_agent(agent_id: int, session: AsyncSession = Depends(get_session)):
    dao = PollingDao(session)
    agent = await dao.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Агента не знайдено")
    await dao.delete_agent(agent)
    await session.commit()


@router.get(
    "/agents/{agent_id}/devices",
    response_model=List[int],
    dependencies=[Depends(require_admin)],
)
async def get_agent_devices(
    agent_id: int, session: AsyncSession = Depends(get_session)
):
    return await PollingDao(session).agent_devices(agent_id)


@router.put(
    "/agents/{agent_id}/devices",
    response_model=List[int],
    dependencies=[Depends(require_admin)],
)
async def set_agent_devices(
    agent_id: int,
    body: AgentDevices,
    session: AsyncSession = Depends(get_session),
):
    dao = PollingDao(session)
    if await dao.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="Агента не знайдено")
    try:
        await dao.set_agent_devices(agent_id, body.device_ids)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="У списку є прилад, якого не існує",
        )
    return await dao.agent_devices(agent_id)


# ── Default hours ─────────────────────────────────────────────────────────────

@router.get("/schedule", response_model=ScheduleRead)
async def get_schedule(session: AsyncSession = Depends(get_session)):
    settings = await PollingDao(session).get_settings()
    await session.commit()
    return ScheduleRead(poll_times=settings.poll_times)


@router.put(
    "/schedule",
    response_model=ScheduleRead,
    dependencies=[Depends(require_admin)],
)
async def set_schedule(
    body: ScheduleRead, session: AsyncSession = Depends(get_session)
):
    times = _clean({"poll_times": body.poll_times})["poll_times"]
    settings = await PollingDao(session).set_poll_times(times)
    await session.commit()
    return ScheduleRead(poll_times=settings.poll_times)
