"""GSM polling schema; DPD archives keep everything and record their source

Step 1 of docs/plans/gsm-polling.md.

Two independent changes ride together on purpose. The polling tables are new
and touch nothing. The `source` column is not new work — it has to exist
before the first GSM row is written, because UNIQUE(device_id, stamp) leaves
room for one row per hour, and without a way to tell the two sources apart the
nightly DPD refresh would overwrite what a modem read off the device.

Retention goes at the same time. These tables were a cache while the DPD API
was the only source: a dropped row could always be re-fetched. A row the modem
brought cannot — the device keeps weeks, not years. Measured 07.09.2026:
~292 B per hourly record, about 1.25 GB a year at full coverage, against a
748 MB database. Cheaper to keep everything than to delete what nobody can
restore. `accessed_at` existed only to feed the prune and costs a write on
every read, so it goes with it.

Revision ID: e2a4c6b8d0f3
Revises: d3f7a9c15e28
Create Date: 2026-09-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2a4c6b8d0f3"
down_revision: Union[str, None] = "d3f7a9c15e28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ARCHIVES = ("dpd_daily_archive", "dpd_hourly_archive")


def upgrade() -> None:
    # ── DPD archives: keep everything, remember who wrote it ─────────────────
    for table in _ARCHIVES:
        op.add_column(
            table,
            sa.Column(
                "source",
                sa.String(length=8),
                nullable=False,
                server_default="dpd",
            ),
        )
        # Everything already there came from the API, which the default states.
        op.drop_column(table, "accessed_at")

    # ── Who may poll ─────────────────────────────────────────────────────────
    op.create_table(
        "poll_agent",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False,
                  server_default="workstation"),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("branch_id", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["branch_id"], ["grmu_branch.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_poll_agent_name"),
    )
    op.create_index("idx_poll_agent_active", "poll_agent", ["active"])
    op.create_index("ix_poll_agent_branch_id", "poll_agent", ["branch_id"])

    # ── The corrector card: how to reach it, and what happened last ──────────
    op.create_table(
        "poll_device",
        sa.Column("id", sa.BigInteger(), nullable=False),
        # Exactly one target of three.
        sa.Column("gas_volume_calc_id", sa.BigInteger(), nullable=True),
        sa.Column("dpd_line_id", sa.BigInteger(), nullable=True),
        sa.Column("dpd_device_id", sa.BigInteger(), nullable=True),
        # Schedule.
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_poll", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("poll_times", postgresql.JSONB(), nullable=True),
        # Link.
        sa.Column("channel", sa.String(length=8), nullable=False, server_default="com"),
        sa.Column("is_modem", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("init_str", sa.String(length=64), nullable=False,
                  server_default="AT&F"),
        sa.Column("dial_prefix", sa.String(length=16), nullable=False,
                  server_default="ATDP"),
        sa.Column("baud", sa.Integer(), nullable=False, server_default="9600"),
        sa.Column("tcp_host", sa.String(length=255), nullable=True),
        sa.Column("tcp_port", sa.Integer(), nullable=True),
        # Protocol.
        sa.Column("protocol_id", sa.Integer(), nullable=True),
        sa.Column("device_address", sa.Integer(), nullable=True),
        sa.Column("answer_timeout_sec", sa.Integer(), nullable=False,
                  server_default="7"),
        sa.Column("pause_between_ms", sa.Integer(), nullable=False,
                  server_default="400"),
        sa.Column("repeat_count", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("preamble_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("depth_days", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(length=500), nullable=True),
        # Modem (a copy of cask2.Devices.COMChannel).
        sa.Column("modem_connect_timeout_sec", sa.Integer(), nullable=False,
                  server_default="40"),
        sa.Column("modem_repeat_call_count", sa.Integer(), nullable=False,
                  server_default="3"),
        sa.Column("modem_pause_after_connect_ms", sa.Integer(), nullable=False,
                  server_default="400"),
        # Adapter / radio: carried over from ask2cfg.xml, unused for now.
        sa.Column("is_adapter", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("adapter_line", sa.Integer(), nullable=True),
        sa.Column("adapter_speed", sa.Integer(), nullable=True),
        sa.Column("adapter_level_in", sa.Integer(), nullable=True),
        sa.Column("adapter_level_out", sa.Integer(), nullable=True),
        sa.Column("adapter_is_frequency", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("adapter_is_radio", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        # State of the last session.
        sa.Column("last_poll_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=8), nullable=True),
        sa.Column("last_error_code", sa.String(length=32), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("last_agent_id", sa.BigInteger(), nullable=True),
        sa.Column("last_rows", postgresql.JSONB(), nullable=False,
                  server_default="{}"),
        sa.Column("last_duration_ms", sa.Integer(), nullable=True),
        sa.Column("last_connect_ms", sa.Integer(), nullable=True),
        # Manual request.
        sa.Column("manual_requested_at", sa.DateTime(), nullable=True),
        sa.Column("manual_requested_by", sa.BigInteger(), nullable=True),
        # Soft claim.
        sa.Column("polling_agent_id", sa.BigInteger(), nullable=True),
        sa.Column("polling_since", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["gas_volume_calc_id"], ["gas_volume_calc.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dpd_line_id"], ["dpd_line.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dpd_device_id"], ["dpd_device.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_agent_id"], ["poll_agent.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["manual_requested_by"], ["app_user.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["polling_agent_id"], ["poll_agent.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_check_constraint(
        "ck_poll_device_single_target",
        "poll_device",
        "(gas_volume_calc_id IS NOT NULL)::int "
        "+ (dpd_line_id IS NOT NULL)::int "
        "+ (dpd_device_id IS NOT NULL)::int = 1",
    )
    # One card per corrector. Partial, because two of the three target columns
    # are always NULL and a plain unique index would allow only one such row.
    op.create_index("uq_poll_device_calc", "poll_device", ["gas_volume_calc_id"],
                    unique=True,
                    postgresql_where=sa.text("gas_volume_calc_id IS NOT NULL"))
    op.create_index("uq_poll_device_dpd_line", "poll_device", ["dpd_line_id"],
                    unique=True,
                    postgresql_where=sa.text("dpd_line_id IS NOT NULL"))
    op.create_index("uq_poll_device_dpd_device", "poll_device", ["dpd_device_id"],
                    unique=True,
                    postgresql_where=sa.text("dpd_device_id IS NOT NULL"))
    op.create_index("idx_poll_device_due", "poll_device",
                    ["enabled", "auto_poll", "last_poll_at"])

    # ── Who took which devices ───────────────────────────────────────────────
    op.create_table(
        "poll_agent_device",
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("poll_device_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["poll_agent.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["poll_device_id"], ["poll_device.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id", "poll_device_id"),
    )

    # ── Log of the current session only ──────────────────────────────────────
    op.create_table(
        "poll_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("poll_device_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False,
                  server_default="info"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["poll_device_id"], ["poll_device.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_poll_log_device_seq", "poll_log",
                    ["poll_device_id", "seq"])

    # ── One short row per attempt ────────────────────────────────────────────
    op.create_table(
        "poll_attempt",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("poll_device_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False,
                  server_default="error"),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("rows", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["poll_device_id"], ["poll_device.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["poll_agent.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_poll_attempt_device_started", "poll_attempt",
                    ["poll_device_id", "started_at"])

    # ── Default poll hours (single row) ──────────────────────────────────────
    op.create_table(
        "poll_settings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("poll_times", postgresql.JSONB(), nullable=False,
                  server_default='["06:00"]'),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO poll_settings (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("poll_settings")
    op.drop_index("idx_poll_attempt_device_started", table_name="poll_attempt")
    op.drop_table("poll_attempt")
    op.drop_index("idx_poll_log_device_seq", table_name="poll_log")
    op.drop_table("poll_log")
    op.drop_table("poll_agent_device")
    op.drop_index("idx_poll_device_due", table_name="poll_device")
    op.drop_index("uq_poll_device_dpd_device", table_name="poll_device")
    op.drop_index("uq_poll_device_dpd_line", table_name="poll_device")
    op.drop_index("uq_poll_device_calc", table_name="poll_device")
    op.drop_constraint("ck_poll_device_single_target", "poll_device", type_="check")
    op.drop_table("poll_device")
    op.drop_index("ix_poll_agent_branch_id", table_name="poll_agent")
    op.drop_index("idx_poll_agent_active", table_name="poll_agent")
    op.drop_table("poll_agent")

    # Retention comes back with the column it needs. Rows written by a GSM
    # poll are indistinguishable once `source` is gone, and `accessed_at`
    # starts everyone at today — a year of grace before the prune that this
    # downgrade restores could touch anything.
    for table in _ARCHIVES:
        op.add_column(
            table,
            sa.Column("accessed_at", sa.Date(), nullable=False,
                      server_default=sa.text("CURRENT_DATE")),
        )
        op.alter_column(table, "accessed_at", server_default=None)
        op.drop_column(table, "source")
