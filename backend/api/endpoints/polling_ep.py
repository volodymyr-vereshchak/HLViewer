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
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_current_user, require_admin
from backend.db.dao.polling_dao import PollingDao
from backend.db.engine import get_session
from backend.db.models.app_user_model import AppUser
from backend.db.models.polling_model import PollDevice

router = APIRouter(
    prefix="/polling",
    tags=["polling"],
    dependencies=[Depends(get_current_user)],
)

TARGET_KINDS = ("enterprise", "dpd_line")


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

    protocol_id: Optional[int] = None
    device_address: Optional[int] = None
    answer_timeout_sec: int = 7
    pause_between_ms: int = 400
    repeat_count: int = 3
    preamble_count: int = 0
    depth_days: Optional[int] = None
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
    enterprise_id: Optional[int] = None
    dpd_line_id: Optional[int] = None
    # Belong to the site, offered here because this is where they are decided.
    poll_dpd: Optional[bool] = None
    poll_gsm: Optional[bool] = None

    @model_validator(mode="after")
    def exactly_one_target(self):
        if (self.enterprise_id is None) == (self.dpd_line_id is None):
            # The database says the same thing, but an IntegrityError here
            # would reach the operator as "500" instead of a sentence.
            raise ValueError(
                "Вкажіть рівно одну ціль: підприємство або лінію ДПД"
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
    protocol_id: Optional[int] = None
    device_address: Optional[int] = None
    answer_timeout_sec: Optional[int] = None
    pause_between_ms: Optional[int] = None
    repeat_count: Optional[int] = None
    preamble_count: Optional[int] = None
    depth_days: Optional[int] = None
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
    poll_dpd: Optional[bool] = None
    poll_gsm: Optional[bool] = None


class PollDeviceRead(PollDeviceLink):
    id: int
    enterprise_id: Optional[int] = None
    dpd_line_id: Optional[int] = None
    target_kind: str
    target_label: Optional[str] = None
    # How the site is read. Both may be on; at least one always is.
    poll_dpd: bool = True
    poll_gsm: bool = False
    # The corrector standing at the point right now — what the modem expects to
    # find. None for a DPD line, and for a point currently without one.
    device_id: Optional[int] = None
    device_ser_num: Optional[int] = None
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
    kind: str = "workstation"
    branch_id: Optional[int] = None
    active: bool = True


class PollAgentUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    name: Optional[str] = None
    kind: Optional[str] = None
    branch_id: Optional[int] = None
    active: Optional[bool] = None


class PollAgentRead(BaseModel):
    id: int
    name: str
    kind: str
    branch_id: Optional[int] = None
    active: bool
    last_seen_at: Optional[datetime] = None
    version: Optional[str] = None
    host: Optional[str] = None
    device_count: int = 0


class PollAgentCreated(PollAgentRead):
    """The one response that carries the key in clear."""

    key: str


class AgentDevices(BaseModel):
    device_ids: List[int]


class ScheduleRead(BaseModel):
    poll_times: List[str]


def _read(row: dict) -> PollDeviceRead:
    card: PollDevice = row["card"]
    return PollDeviceRead(
        **card.model_dump(exclude={"created_at", "updated_at"}),
        target_kind=row["target_kind"],
        target_label=row["target_label"],
        poll_dpd=row["poll_dpd"],
        poll_gsm=row["poll_gsm"],
        device_id=row["device_id"],
        device_ser_num=row["device_ser_num"],
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


def _valid_times(poll_times: Optional[List[str]]) -> None:
    """"25:00" is not a time, and the agent would silently never reach it."""
    for value in poll_times or []:
        try:
            hour, minute = value.split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Некоректний час опитування: {value!r}. Очікується HH:MM",
            )


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
    _valid_times(body.poll_times)
    dao = PollingDao(session)
    payload = body.model_dump()
    poll_dpd = payload.pop("poll_dpd", None)
    poll_gsm = payload.pop("poll_gsm", None)
    try:
        card = await dao.create_device(payload)
        await dao.set_poll_paths(card, poll_dpd, poll_gsm)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Для цього об'єкта вже є картка опитування",
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
    patch = body.model_dump(exclude_unset=True)
    if "poll_times" in patch:
        _valid_times(patch["poll_times"])
    poll_dpd = patch.pop("poll_dpd", None)
    poll_gsm = patch.pop("poll_gsm", None)
    if poll_dpd is not None or poll_gsm is not None:
        await dao.set_poll_paths(card, poll_dpd, poll_gsm)
    await dao.update_device(card, patch)
    await session.commit()
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


# ── Agents ────────────────────────────────────────────────────────────────────

@router.get(
    "/agents",
    response_model=List[PollAgentRead],
    dependencies=[Depends(require_admin)],
)
async def list_agents(session: AsyncSession = Depends(get_session)):
    return [
        PollAgentRead(**row["agent"].model_dump(), device_count=row["device_count"])
        for row in await PollingDao(session).list_agents()
    ]


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
    _valid_times(body.poll_times)
    settings = await PollingDao(session).set_poll_times(body.poll_times)
    await session.commit()
    return ScheduleRead(poll_times=settings.poll_times)
