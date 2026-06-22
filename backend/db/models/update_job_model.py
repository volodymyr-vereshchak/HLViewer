from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class UpdateJob(SQLModel, table=True):
    """Single-row table (id=1) holding the global hostlib-update job state.

    This is the source of truth shared across all uvicorn workers. The job
    state used to live in a module-global dict, which broke with --workers > 1
    because each worker process has its own memory (status polls hit a random
    worker and saw stale 'idle' state, the 429 guard failed, duplicate updates
    started). Storing it in Postgres makes all workers agree.

    `updated_at` is a heartbeat written every ~1.5s while a job runs; if it goes
    stale the job is considered crashed and can be taken over / reset.
    """
    __tablename__ = "update_job"

    id: int = Field(default=1, primary_key=True, sa_type=BigInteger)
    status: str = Field(default="idle", max_length=20)  # idle | running | done | error
    started_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    finished_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    updated_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error: Optional[str] = Field(default=None, max_length=2000)
    lumg_id: Optional[int] = Field(default=None, sa_type=BigInteger)
    progress: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
