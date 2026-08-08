"""Gas transport routes: a set of lines carrying the SAME gas.

Along a route the physical-chemical properties (ФХП) must be identical, so a
route is the unit the «Звірка ФХП» report compares within. One or more of its
lines are marked as the REFERENCE — the composition the rest are checked
against. Usually that is a line with a stream chromatograph, which updates the
composition by itself while the others are typed in by hand; but a route
without a chromatograph still needs a reference, so the flag says "this is the
reference", not "this has a chromatograph".

A line belongs to at most ONE route — `uq_gas_route_member_line` — which is the
deliberate difference from `VirtualLineMember` (a line may sit in several
rings). Two routes claiming the same line would mean the same gas measured
against two different references.

DPD lines are not members: they have no `edit_archive`, so there is no ФХП
history to read for them.
"""

from typing import List, Optional

from sqlalchemy import BigInteger, Index
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from .base_model import HlBaseModel


class GasRouteBase(HlBaseModel):
    # Text, not a number: keeps leading zeros ("007"), survives "301А" if such
    # a number ever appears, and at fixed width sorts the same as an integer.
    number: str = Field(max_length=16)
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    active: bool = Field(default=True)


class GasRoute(GasRouteBase, table=True):
    __tablename__ = "gas_route"
    __table_args__ = (
        UniqueConstraint("branch_id", "number", name="uq_gas_route_branch_number"),
        Index("idx_gas_route_branch", "branch_id"),
        Index("idx_gas_route_active", "active"),
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    branch_id: int = Field(
        foreign_key="grmu_branch.id", ondelete="CASCADE", sa_type=BigInteger,
    )

    # No `branch` relationship on purpose: nothing needs the reverse traversal,
    # and leaving it out keeps GrmuBranch untouched. The cascade is enforced by
    # the foreign key itself.
    members: List["GasRouteMember"] = Relationship(
        back_populates="route", cascade_delete=True
    )


class GasRouteMember(HlBaseModel, table=True):
    __tablename__ = "gas_route_member"
    __table_args__ = (
        # A line belongs to at most one route — the whole point of the table.
        UniqueConstraint("line_id", name="uq_gas_route_member_line"),
        Index("idx_gas_route_member_route", "route_id"),
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=BigInteger)
    route_id: int = Field(
        foreign_key="gas_route.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    line_id: int = Field(
        foreign_key="gas_volume_line.id", ondelete="CASCADE", sa_type=BigInteger,
    )
    # "This line's ФХП is the reference for the route." Lives here rather than
    # on `gas_volume_line` because it is a decision about THIS route — the same
    # line could be the reference in one context and the compared one in
    # another — and because the flag is meaningless for a line in no route.
    # A route must have at least one; the endpoint refuses to save otherwise.
    is_reference: bool = Field(default=False)
    sort_order: int = Field(default=0)

    route: "GasRoute" = Relationship(back_populates="members")


# ─── API models ───────────────────────────────────────────────────────────────


class GasRouteMemberIn(SQLModel):
    line_id: int
    is_reference: bool = False


class GasRouteMemberOut(GasRouteMemberIn):
    id: int
    line_name: str | None = None
    sort_order: int = 0


class GasRouteFields(SQLModel):
    """The route's own columns, without the timestamps the table carries."""

    number: str
    name: Optional[str] = None
    description: Optional[str] = None
    active: bool = True


class GasRouteRead(GasRouteFields):
    id: int
    branch_id: int
    members: list[GasRouteMemberOut] = []


class GasRouteWrite(GasRouteFields):
    branch_id: int
    # Order in this list becomes sort_order; PATCH replaces the whole set.
    members: list[GasRouteMemberIn] = []


class FreeLine(SQLModel):
    """A line of the branch that no OTHER route has claimed."""

    id: int
    name: str
    calc_name: str | None = None
