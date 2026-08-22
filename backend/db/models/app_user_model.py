from typing import List, Optional

from sqlalchemy import BigInteger
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
    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    password_hash: Optional[str] = Field(default=None, max_length=255)
    # Bumped whenever an admin changes what this account may do (role, active,
    # branch access) or resets its password. The value is baked into the JWT at
    # login; a token carrying an older one is refused, so a rights change takes
    # effect on the next request instead of waiting out a token that can live
    # 30 days. See auth_ep.resolve_session_user.
    perms_version: int = Field(default=1, nullable=False)
    branch_access: List["AppUserBranchAccess"] = Relationship(back_populates="user")


class AppUserBranchAccess(HlBaseModel, table=True):
    __tablename__ = "app_user_branch_access"
    __table_args__ = (
        UniqueConstraint("user_id", "branch_id", name="uq_app_user_branch_access"),
    )
    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    user_id: int = Field(
        foreign_key="app_user.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    branch_id: int = Field(
        foreign_key="grmu_branch.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    user: Optional[AppUser] = Relationship(back_populates="branch_access")


class AppUserRead(AppUserBase):
    id: int
    allowed_branch_ids: list[int] = []
    # Whether this account has a password of ours at all. False = a domain
    # account: it was provisioned by an LDAP login and authenticates against
    # Active Directory, so there is nothing here for an admin to reset. Derived
    # from password_hash, which itself never leaves the backend.
    has_password: bool = True
