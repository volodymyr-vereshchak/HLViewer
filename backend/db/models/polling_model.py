"""Polling gas meters over a GSM modem: agents, devices, state, logs.

Plan: `docs/plans/gsm-polling.md`. Schema only — "is it due", the agent
endpoints and the agent itself are the next steps.

Two facts shape every table below:

* **The poll runs on a client machine, not on the server.** The Ask2 drivers
  are .NET Framework assemblies that reference WPF; they cannot be loaded into
  a python process at all, and not on Linux under any runtime. So the server
  knows exactly what the agents tell it, and the schema has no run queue, no
  COM ports and no modem-busy counters: there is nothing the server could do
  with them.
* **State lives on the device, not in a history of runs.** "Due" is computed
  from `last_poll_at` and the device's own hours, which is what keeps an agent
  going offline from blocking anybody: the device simply stays overdue and
  falls to whoever can reach it.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, Column, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel, UniqueConstraint

from .base_model import HlBaseModel


class PollAgent(HlBaseModel, table=True):
    """A machine allowed to poll devices.

    The key is shown once when the agent is created and stored as a hash, the
    way a user password is. The agent presents it in an `X-Agent-Key` header
    and has no cookie: it is a service, not a browser.
    """

    __tablename__ = "poll_agent"
    __table_args__ = (
        UniqueConstraint("name", name="uq_poll_agent_name"),
        Index("idx_poll_agent_active", "active"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    name: str = Field(max_length=100)
    key_hash: str = Field(max_length=255)
    branch_id: Optional[int] = Field(
        default=None, foreign_key="grmu_branch.id", ondelete="SET NULL",
        index=True, sa_type=BigInteger,
    )
    active: bool = Field(default=True)

    # Self-description from `hello`: what the agent last said about itself.
    last_seen_at: Optional[datetime] = Field(default=None)
    version: Optional[str] = Field(default=None, max_length=32)
    host: Optional[str] = Field(default=None, max_length=255)


class PollDevice(HlBaseModel, table=True):
    """A phone number bound to the corrector it reaches.

    The card names the CORRECTOR, and the operator moves it when the device is
    replaced: same phone, new serial, and from that moment the modem reads the
    new one. The point's own history stays continuous in `enterprise_device`,
    which is where it belongs — the poll has no opinion about it.

    Binding to the corrector rather than to the site is what makes the reply
    checkable. The device reports its serial; if it is not the one this card
    names, nothing is written and the operator is told, because that is either
    a replacement nobody entered or a call that reached the wrong place.

    Two kinds of corrector, exactly one set (`ck_poll_device_single_target`,
    the `virtual_line_member` idiom): one standing at an enterprise metering
    point (`dpd_device`), or one on a DPD line — whose history keeps the serial
    inline rather than as a device row, so the card points at the line and the
    expected serial is read from its current entry. ЛУМГ correctors are not
    polled over GSM at all: Ask2 keeps doing that and writing its hostlib
    files.

    The link fields are a deliberate copy of `ask2cfg.xml`, adapter and radio
    included, even though neither is used today. Otherwise migrating the
    settings out of Ask2 would be selective rather than mechanical, and every
    column left out would surface on the first device that needs it.
    """

    __tablename__ = "poll_device"
    __table_args__ = (
        CheckConstraint(
            "(dpd_device_id IS NOT NULL)::int"
            " + (dpd_line_id IS NOT NULL)::int"
            " + (enterprise_id IS NOT NULL)::int = 1",
            name="ck_poll_device_single_target",
        ),
        # One card per corrector. The indexes are partial because one of the
        # two target columns is always NULL, and a plain unique index would
        # then allow only one such row in the whole table.
        Index(
            "uq_poll_device_dpd_device", "dpd_device_id",
            unique=True, postgresql_where=Column("dpd_device_id").isnot(None),
        ),
        Index(
            "uq_poll_device_dpd_line", "dpd_line_id",
            unique=True, postgresql_where=Column("dpd_line_id").isnot(None),
        ),
        Index(
            "uq_poll_device_enterprise", "enterprise_id",
            unique=True, postgresql_where=Column("enterprise_id").isnot(None),
        ),
        # What the agent's plan query filters on: enabled, automatic, overdue.
        Index("idx_poll_device_due", "enabled", "auto_poll", "last_poll_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)

    # ── Target: the corrector, exactly one of the two kinds ──────────────────
    # Editable, unlike most foreign keys here: repointing the card at the new
    # serial IS how a replacement is recorded on this screen.
    dpd_device_id: Optional[int] = Field(
        default=None, foreign_key="dpd_device.id", ondelete="CASCADE",
        sa_type=BigInteger,
    )
    dpd_line_id: Optional[int] = Field(
        default=None, foreign_key="dpd_line.id", ondelete="CASCADE",
        sa_type=BigInteger,
    )
    # The third kind, and the one operators actually fill in: the modem stands
    # at the enterprise, not inside the corrector. Which corrector to read is
    # then not a setting at all — it is whichever one is installed there now,
    # resolved at poll time from the device history. A replacement stops being
    # something anybody has to remember to repoint.
    enterprise_id: Optional[int] = Field(
        default=None, foreign_key="enterprise.id", ondelete="CASCADE",
        sa_type=BigInteger,
    )

    # ── Schedule ─────────────────────────────────────────────────────────────
    enabled: bool = Field(default=True)
    # Whether to poll it automatically. A device with this off is still
    # available for a manual "poll now" — those are different questions.
    auto_poll: bool = Field(default=True)
    # A list of "HH:MM". NULL means "use the global hours from poll_settings":
    # those became a default rather than the only option, because the usual
    # rhythm is once or twice a day but a particular device may need more.
    poll_times: Optional[list] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    # ── Link ─────────────────────────────────────────────────────────────────
    channel: str = Field(default="com", max_length=8)  # com | tcp
    is_modem: bool = Field(default=True)
    # Always +380 and nine digits (see services/poll_validation). The agent
    # hands this straight to ATDP, and a modem does not guess: "050…" reaches
    # nothing, and the failure surfaces as "no dialtone" on somebody else's
    # machine hours later.
    phone: Optional[str] = Field(default=None, max_length=16)
    init_str: str = Field(default="AT&F", max_length=64)
    dial_prefix: str = Field(default="ATDP", max_length=16)
    # No baud. Ask2 kept it per device (COMChannel.bitRate belongs to Calc),
    # but across this fleet it is one speed for every poll, so it lives in the
    # agent beside the COM port it applies to — the server has nothing to do
    # with either. If one device ever needs its own, the column comes back.
    tcp_host: Optional[str] = Field(default=None, max_length=255)
    tcp_port: Optional[int] = Field(default=None)

    # ── Protocol ─────────────────────────────────────────────────────────────
    # Ask2 driver id (1070 Floutec BP-2, 1052 KPLG, 1054 Vega…). To be filled
    # in from the corrector type through the corector_type → gas_vol_calc_type
    # bridge rather than asked for; the bridge arrives with step 2.
    protocol_id: Optional[int] = Field(default=None)
    # The address in the request frame. Every driver sends it and checks it in
    # the reply — Vega puts it in byte 0 of a Modbus frame and refuses an
    # answer that comes back under another address ("помилка адреси"). With one
    # device on a line it stays at its default, which is why it looks nominal,
    # but sending the wrong one looks exactly like a dead meter. Filled in, not
    # asked: for a ЛУМГ corrector it is the number in the hostlib file name.
    device_address: Optional[int] = Field(default=None)
    # No access code or password here. In these protocols they buy the right to
    # WRITE — set the clock, change the contract hour, load an FHP passport —
    # and this poll only reads archives. Carrying a plaintext device credential
    # we never send would be a liability with no use: it would be filled in by
    # the ask2cfg.xml import, backed up and bundled to the offline server for
    # nothing. The values stay in ask2cfg.xml if writing is ever added.

    answer_timeout_sec: int = Field(default=7)
    pause_between_ms: int = Field(default=400)
    repeat_count: int = Field(default=3)
    preamble_count: int = Field(default=0)
    # No depth setting. The first poll of a device takes everything its archive
    # still holds, and later ones fill in the days and hours that are missing —
    # the driver asks our stub for the last stored date and reads from there.
    # A number of days would only ever be a way to fetch less than everything,
    # and a GSM poll has no second chance: the corrector keeps weeks, so a
    # window nobody polled in time is gone for good.
    #
    # 0…5, 0 first — a queue order an operator compares by eye rather than a
    # free integer, because "priority 900" says nothing about where it sits.
    priority: int = Field(default=0)
    note: Optional[str] = Field(default=None, max_length=500)

    # ── Modem (a copy of cask2.Devices.COMChannel) ───────────────────────────
    modem_connect_timeout_sec: int = Field(default=40)
    modem_repeat_call_count: int = Field(default=3)
    modem_pause_after_connect_ms: int = Field(default=400)

    # ── Adapter and radio: carried over for the migration, unused for now ────
    is_adapter: bool = Field(default=False)
    adapter_line: Optional[int] = Field(default=None)
    adapter_speed: Optional[int] = Field(default=None)
    adapter_level_in: Optional[int] = Field(default=None)
    adapter_level_out: Optional[int] = Field(default=None)
    adapter_is_frequency: bool = Field(default=False)
    adapter_is_radio: bool = Field(default=False)

    # ── State of the last session ────────────────────────────────────────────
    # The only thing "done" is counted from: a success, not an attempt.
    last_poll_at: Optional[datetime] = Field(default=None)
    last_attempt_at: Optional[datetime] = Field(default=None)
    last_status: Optional[str] = Field(default=None, max_length=8)  # ok | error
    # no_carrier | busy | no_dialtone | no_answer | timeout | crc | auth | …
    last_error_code: Optional[str] = Field(default=None, max_length=32)
    last_error_text: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    last_agent_id: Optional[int] = Field(
        default=None, foreign_key="poll_agent.id", ondelete="SET NULL",
        sa_type=BigInteger,
    )
    # Rows the last poll brought, per archive: {"hour": 168, "day": 7, …}
    last_rows: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    # How far the running session has got. Set by the agent as it reads, and
    # the only thing that turns "дзвоню" into something an operator can wait
    # through: a poll of a month of hours is seven hundred requests, and a
    # screen that says nothing for ten minutes reads as a screen that hung.
    progress_done: Optional[int] = Field(default=None)
    progress_total: Optional[int] = Field(default=None)
    #: Which archive those numbers are counting — "hourly", "daily", or None
    #: before the reading starts. A count with no unit is worse than none: the
    #: bar said "прочитано годин" all the way through the daily ring, and the
    #: numbers under it were days.
    progress_phase: Optional[str] = Field(default=None, max_length=16)

    #: Set when somebody asks the running call to stop. The agent reads it on
    #: its next log push — a second or two — and hangs up between records,
    #: which is the only moment at which nothing is half-read.
    cancel_requested_at: Optional[datetime] = Field(default=None)

    last_duration_ms: Optional[int] = Field(default=None)
    last_connect_ms: Optional[int] = Field(default=None)

    # ── Request for an out-of-turn poll ──────────────────────────────────────
    manual_requested_at: Optional[datetime] = Field(default=None)
    manual_requested_by: Optional[int] = Field(
        default=None, foreign_key="app_user.id", ondelete="SET NULL",
        sa_type=BigInteger,
    )

    # ── Soft claim, held for the session ─────────────────────────────────────
    # Needed for exactly one case: a device two operators both ticked, whose
    # agents both decide at the same moment that it is overdue. The claim is
    # taken with one atomic UPDATE and releases itself after 20 minutes if the
    # agent was switched off mid-session. This is the only synchronisation
    # between agents in the whole schema.
    polling_agent_id: Optional[int] = Field(
        default=None, foreign_key="poll_agent.id", ondelete="SET NULL",
        sa_type=BigInteger,
    )
    polling_since: Optional[datetime] = Field(default=None)


class PollAgentDevice(SQLModel, table=True):
    """Who took which devices. Many-to-many, and not for future-proofing.

    A workstation usually has one modem, and there are several operators: the
    backend hands down the list of devices and each agent ticks the ones it
    takes. The operators split the work between themselves; the administrator
    does not assign anything.

    Two consequences follow, both visible in the UI:
      * a device nobody ticked is never polled — silent data loss, so it needs
        an explicit "unassigned" indicator;
      * a device two agents ticked goes to whichever got there first; the
        other sees the updated `last_poll_at` and skips it.
    """

    __tablename__ = "poll_agent_device"

    agent_id: int = Field(
        foreign_key="poll_agent.id", ondelete="CASCADE",
        primary_key=True, sa_type=BigInteger,
    )
    poll_device_id: int = Field(
        foreign_key="poll_device.id", ondelete="CASCADE",
        primary_key=True, sa_type=BigInteger,
    )


class PollAttempt(SQLModel, table=True):
    """One short row per poll attempt.

    The full log is kept for the last session only, as asked. But then there
    is nothing to answer "who fails to respond, and how often" — which is the
    question that started this whole feature. This is the compromise: no text,
    but the fact survives. A few hundred rows a day for the entire fleet.
    """

    __tablename__ = "poll_attempt"
    __table_args__ = (
        Index("idx_poll_attempt_device_started", "poll_device_id", "started_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    poll_device_id: int = Field(
        foreign_key="poll_device.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    agent_id: Optional[int] = Field(
        default=None, foreign_key="poll_agent.id", ondelete="SET NULL",
        sa_type=BigInteger,
    )
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = Field(default=None)
    status: str = Field(default="error", max_length=8)  # ok | error
    # Asked for by a person rather than made by the schedule. The retry budget
    # counts scheduled attempts only: checking a site by hand must not use up
    # the automatic calls it still had.
    manual: bool = Field(default=False)
    error_code: Optional[str] = Field(default=None, max_length=32)
    rows: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )


class PollSettings(SQLModel, table=True):
    """Single row (id=1): the default poll hours.

    A device with its own `poll_times` never reads these. They are a default,
    not the only option: the usual rhythm is once or twice a day, but a given
    device can be polled more often or inside its own window.
    """

    __tablename__ = "poll_settings"

    id: int = Field(default=1, primary_key=True, sa_type=BigInteger)
    poll_times: list = Field(
        default_factory=lambda: ["06:00"],
        sa_column=Column(JSONB, nullable=False, server_default='["06:00"]'),
    )
