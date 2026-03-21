from typing import List, Optional

from sqlmodel import Field, Relationship, UniqueConstraint, Index

from .base_model import HlBaseModel


class AppUserBase(HlBaseModel):
    username: str = Field(max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)
    role: str = Field(default="viewer", max_length=32)
    active: bool = Field(default=True)


class AppUser(AppUserBase, table=True):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("username", name="uq_app_user_username"),
        Index("idx_app_user_username", "username"),
        Index("idx_app_user_role", "role"),
        Index("idx_app_user_active", "active"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    password_hash: Optional[str] = Field(default=None, max_length=255)
    branch_access: List["AppUserBranchAccess"] = Relationship(back_populates="user")


class AppUserBranchAccess(HlBaseModel, table=True):
    __tablename__ = "app_user_branch_access"
    __table_args__ = (
        UniqueConstraint("user_id", "branch_id", name="uq_app_user_branch_access"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="app_user.id", ondelete="CASCADE")
    branch_id: int = Field(foreign_key="grmu_branch.id", ondelete="CASCADE")
    user: Optional[AppUser] = Relationship(back_populates="branch_access")


class AppUserRead(AppUserBase):
    id: int
    allowed_branch_ids: list[int] = []
