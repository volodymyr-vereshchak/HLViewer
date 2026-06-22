from sqlmodel import Field
from sqlalchemy import Index, BigInteger

from .base_model import HlBaseModel


class TelegramUserBase(HlBaseModel):
    user_id: int = Field(default=None, unique=True, sa_type=BigInteger)
    active: bool


class TelegramUser(TelegramUserBase, table=True):
    __tablename__ = "telegram_users"
    __table_args__ = (
        # Индекс для поиска по user_id
        Index("idx_telegram_user_id", "user_id"),
        # Индекс для поиска активных пользователей
        Index("idx_telegram_active", "active"),
        # Составной индекс для поиска активных пользователей по user_id
        Index("idx_telegram_active_user", "active", "user_id"),
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)


class TelegramUserList(TelegramUserBase):
    id: int


class TelegramUserCreate(TelegramUserBase):
    pass


class TelegramUserUpdate(TelegramUserBase):
    user_id: int | None = None
