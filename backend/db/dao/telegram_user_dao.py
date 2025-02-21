from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import TelegramUserCreate
from backend.db.models.telegram_user_model import TelegramUser, TelegramUserUpdate


class TelegramUserDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = TelegramUser

    async def get_user_by_user_id(self, user_id: int):
        statement = select(self.model).where(self.model.user_id == user_id)
        result = await self.session.execute(statement)
        return result.scalars().first()

    async def activate_user_or_create(self, user_id: int):
        try:
            user_to_activate = await self.get_user_by_user_id(user_id)
            if user_to_activate:
                if not user_to_activate.active:
                    user_update = TelegramUserUpdate(active=True)
                    await self.update_by_id(
                        item_id=user_to_activate.id, item=user_update
                    )
                    return True
                else:
                    return False
            else:
                user_to_activate = TelegramUserCreate(user_id=user_id, active=True)
                await self.create_item(user_to_activate)
                return True
        except Exception as e:
            self.logger.error(
                f"Unexpected error occurred while activate telegram user: {e}",
                exc_info=True,
            )
            raise

    async def deactivate_user_by_user_id(self, user_id: int):
        try:
            user_to_deactivate = await self.get_user_by_user_id(user_id)

            if user_to_deactivate:
                if user_to_deactivate.active:
                    user_update = TelegramUserUpdate(active=False)
                    await self.update_by_id(
                        item_id=user_to_deactivate.id, item=user_update
                    )
                    return True
        except Exception as e:
            self.logger.error(
                f"Unexpected error occurred while deactivate telegram user: {e}",
                exc_info=True,
            )
            raise
        return False

    async def get_all_user_ids(self):
        statement = select(self.model.user_id).where(self.model.active)
        result = await self.session.execute(statement)
        ids_list = result.scalars().all()
        return ids_list
