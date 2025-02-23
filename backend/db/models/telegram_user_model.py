from sqlmodel import Field

from .base_model import HlBaseModel


class TelegramUserBase(HlBaseModel):
    user_id: int = Field(default=None, unique=True)
    active: bool


class TelegramUser(TelegramUserBase, table=True):
    __tablename__ = "telegram_users"

    id: int | None = Field(default=None, primary_key=True)


class TelegramUserList(TelegramUserBase):
    id: int


class TelegramUserCreate(TelegramUserBase):
    pass


class TelegramUserUpdate(TelegramUserBase):
    user_id: int | None = None
