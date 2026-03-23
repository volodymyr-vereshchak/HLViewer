from datetime import datetime

from sqlalchemy import func, Column
from sqlmodel import SQLModel
from sqlmodel import Field


# Convention: all datetimes are Europe/Kyiv local time (naive, no tzinfo).
# Server MUST run with TZ=Europe/Kyiv so that datetime.now() is consistent
# with archive period values read from gas meter binary files (local Ukraine time).
class HlBaseModel(SQLModel):
    created_at: datetime = Field(
        default_factory=datetime.now,
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        nullable=False,
        sa_column_kwargs={"onupdate": func.now()},
    )
