from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models.app_user_model import AppUser, AppUserBranchAccess


class AppUserDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = AppUser

    async def get_by_username(self, username: str) -> AppUser | None:
        result = await self.session.execute(
            select(AppUser).where(AppUser.username == username)
        )
        return result.scalar_one_or_none()

    async def get_all_ordered(self):
        result = await self.session.execute(
            select(AppUser).order_by(AppUser.username)
        )
        return result.scalars().all()

    async def branch_ids(self, user_id: int) -> list[int]:
        result = await self.session.execute(
            select(AppUserBranchAccess.branch_id).where(
                AppUserBranchAccess.user_id == user_id
            )
        )
        return list(result.scalars().all())

    async def other_active_admins(self, user_id: int) -> int:
        """Count active admins besides `user_id` — the "never leave the system
        without an active admin" guard used by update_user and delete_user."""
        return (
            await self.session.execute(
                select(func.count()).select_from(AppUser).where(
                    AppUser.role == "admin",
                    AppUser.active == True,  # noqa: E712
                    AppUser.id != user_id,
                )
            )
        ).scalar_one()

    async def set_branch_access(self, user_id: int, branch_ids: list[int]) -> None:
        """Replace the user's branch-access rows. Flushes but does not commit —
        the caller owns the transaction."""
        await self.delete_branch_access(user_id)
        for bid in branch_ids:
            self.session.add(AppUserBranchAccess(user_id=user_id, branch_id=bid))

    async def delete_branch_access(self, user_id: int) -> None:
        existing = await self.session.execute(
            select(AppUserBranchAccess).where(AppUserBranchAccess.user_id == user_id)
        )
        for row in existing.scalars().all():
            await self.session.delete(row)
        # execute DELETEs before any following INSERTs (unique constraint)
        await self.session.flush()
