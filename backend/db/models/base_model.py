from datetime import datetime

from sqlalchemy import func, Column
from sqlmodel import SQLModel
from sqlmodel import Field


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
