from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, asc

from backend.db.dao.basic_dao import BasicDao
from backend.db.models.enterprise_model import (
    DpdDevice,
    Enterprise,
    EnterpriseDevice,
    EnterpriseDeviceIn,
)
from backend.db.models.device_catalog_model import CorectorType, Manufacturer
from backend.services import device_history


class EnterpriseDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = Enterprise

    async def get_all(self) -> list[Enterprise]:
        stmt = (
            select(self.model)
            .order_by(asc(self.model.line_id.is_(None)), asc(self.model.line_id), asc(self.model.enterprise_name))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_branch_ids(self, branch_ids: list[int]) -> list[Enterprise]:
        stmt = (
            select(self.model)
            .where(self.model.branch_id.in_(branch_ids))
            .order_by(asc(self.model.line_id.is_(None)), asc(self.model.line_id), asc(self.model.enterprise_name))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_id(self, enterprise_id: int) -> Enterprise | None:
        return await self.session.get(self.model, enterprise_id)

    async def delete(self, enterprise_id: int) -> bool:
        return await self.delete_item(enterprise_id)

    async def get_active_for_lines(self, line_ids: list[int]) -> list[Enterprise]:
        stmt = (
            select(self.model)
            .where(self.model.active == True)  # noqa: E712
            .where(self.model.line_id.in_(line_ids))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    # ── Device registry ──────────────────────────────────────────────────────

    async def get_or_create_device(
        self,
        ser_num: int,
        corector_type_id: Optional[int],
        ch_num: int,
        mf_dev: Optional[int] = None,
        type_dev: Optional[int] = None,
    ) -> DpdDevice:
        """The corrector row for an identity, created on first sight.

        Administrators type a serial, a model and a channel — they never pick
        from a device list — so the registry fills itself as points are set up.
        """
        stmt = select(DpdDevice).where(
            DpdDevice.ser_num == ser_num, DpdDevice.ch_num == ch_num
        )
        stmt = stmt.where(
            DpdDevice.corector_type_id == corector_type_id
            if corector_type_id is not None
            else DpdDevice.corector_type_id.is_(None)
        )
        existing = (await self.session.execute(stmt)).scalars().first()
        if existing is not None:
            return existing
        device = DpdDevice(
            ser_num=ser_num, corector_type_id=corector_type_id, ch_num=ch_num,
            mf_dev=mf_dev, type_dev=type_dev,
        )
        self.session.add(device)
        await self.session.flush()
        return device

    async def find_device(
        self, ser_num: int, corector_type_id: Optional[int], ch_num: int
    ) -> DpdDevice | None:
        stmt = select(DpdDevice).where(
            DpdDevice.ser_num == ser_num, DpdDevice.ch_num == ch_num
        )
        stmt = stmt.where(
            DpdDevice.corector_type_id == corector_type_id
            if corector_type_id is not None
            else DpdDevice.corector_type_id.is_(None)
        )
        return (await self.session.execute(stmt)).scalars().first()

    # ── Assignment history ───────────────────────────────────────────────────

    async def get_history(self, enterprise_id: int) -> list[EnterpriseDevice]:
        stmt = (
            select(EnterpriseDevice)
            .where(EnterpriseDevice.enterprise_id == enterprise_id)
            .order_by(EnterpriseDevice.installed_from)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_history_resolved(self, enterprise_id: int) -> list[dict]:
        """History with device identity resolved through the catalog, ordered
        by install moment, each entry carrying its derived window end."""
        stmt = (
            select(
                EnterpriseDevice, DpdDevice,
                CorectorType.type_dev, CorectorType.model_name,
                Manufacturer.mf_dev, Manufacturer.short_name,
            )
            .join(DpdDevice, DpdDevice.id == EnterpriseDevice.device_id)
            .outerjoin(CorectorType, DpdDevice.corector_type_id == CorectorType.id)
            .outerjoin(Manufacturer, CorectorType.manufacturer_id == Manufacturer.id)
            .where(EnterpriseDevice.enterprise_id == enterprise_id)
        )
        rows = (await self.session.execute(stmt)).all()
        context = {r[0].id: r for r in rows}

        resolved = []
        for entry, _win_from, win_to in device_history.resolve_windows(
            [r[0] for r in rows]
        ):
            _, device, ct_type_dev, ct_model, mfr_mf_dev, mfr_short = context[entry.id]
            linked = device.corector_type_id is not None
            resolved.append({
                "id": entry.id,
                "device_id": device.id,
                "ser_num": device.ser_num,
                "corector_type_id": device.corector_type_id,
                "ch_num": device.ch_num,
                "installed_from": entry.installed_from,
                "removed_at": entry.removed_at,
                "mf_dev": mfr_mf_dev if linked else device.mf_dev,
                "type_dev": ct_type_dev if linked else device.type_dev,
                "model_name": ct_model,
                "manufacturer_short_name": mfr_short,
                "bound_to": win_to,
            })
        return resolved

    async def replace_history(
        self, enterprise_id: int, devices: Sequence[EnterpriseDeviceIn]
    ) -> None:
        """Set a point's history to exactly `devices`.

        Stamps are forced to hour precision — DPD's hourly records land on the
        hour, so anything finer could not be lined up with them anyway and
        would only make two histories look different when they are not.
        """
        from sqlalchemy import delete as sa_delete

        await self.session.execute(
            sa_delete(EnterpriseDevice).where(
                EnterpriseDevice.enterprise_id == enterprise_id
            )
        )
        for item in devices:
            device = await self.get_or_create_device(
                item.ser_num, item.corector_type_id, item.ch_num,
                mf_dev=getattr(item, "mf_dev", None),
                type_dev=getattr(item, "type_dev", None),
            )
            self.session.add(EnterpriseDevice(
                enterprise_id=enterprise_id,
                device_id=device.id,
                installed_from=_floor_hour(item.installed_from),
                removed_at=_floor_hour(item.removed_at),
            ))
        await self.session.flush()

    async def clashing_points(
        self, enterprise_id: int, devices: Sequence[EnterpriseDeviceIn]
    ) -> list[tuple[int, str]]:
        """(enterprise_id, name) of OTHER points that would hold one of these
        correctors at the same time.

        One corrector cannot stand at two metering points at once; allowing it
        would count the same gas twice in every report that sums a line.
        """
        device_ids = []
        for item in devices:
            device = await self.find_device(
                item.ser_num, item.corector_type_id, item.ch_num
            )
            if device is not None:
                device_ids.append(device.id)
        if not device_ids:
            return []

        stmt = (
            select(EnterpriseDevice, Enterprise.enterprise_name)
            .join(Enterprise, Enterprise.id == EnterpriseDevice.enterprise_id)
            .where(EnterpriseDevice.enterprise_id != enterprise_id)
            .where(EnterpriseDevice.device_id.in_(device_ids))
        )
        others = (await self.session.execute(stmt)).all()
        if not others:
            return []

        # Windows must be resolved WITHIN each point: one point's next install
        # says nothing about when the device left another.
        other_ids = {e.enterprise_id for e, _ in others}
        names = {e.enterprise_id: name for e, name in others}
        windows_by_point: dict = {}
        for other_id in other_ids:
            windows_by_point[other_id] = device_history.resolve_windows(
                await self.get_history(other_id)
            )

        proposed = []
        for item in devices:
            device = await self.find_device(
                item.ser_num, item.corector_type_id, item.ch_num
            )
            if device is None:
                continue
            proposed.append({
                "device_id": device.id,
                "installed_from": _floor_hour(item.installed_from),
                "removed_at": _floor_hour(item.removed_at),
            })
        windows_by_point[enterprise_id] = device_history.resolve_windows(proposed)

        clashes = device_history.find_device_clashes(
            windows_by_point,
            lambda e: e["device_id"] if isinstance(e, dict) else e.device_id,
        )
        hit = {
            point for _device, a, b in clashes for point in (a, b)
            if point != enterprise_id
        }
        return [(p, names.get(p, str(p))) for p in sorted(hit)]

    async def get_by_device(
        self, ser_num: int, mf_dev: int, type_dev: int, ch_num: int,
        at: Optional[datetime] = None,
    ) -> Enterprise | None:
        """The point a corrector served, matched on the EFFECTIVE codes (the
        catalog when linked, the legacy columns otherwise).

        A corrector that moved served several points, so `at` picks the one in
        force then; without it the most recent assignment wins.
        """
        eff_mf = func.coalesce(Manufacturer.mf_dev, DpdDevice.mf_dev)
        eff_type = func.coalesce(CorectorType.type_dev, DpdDevice.type_dev)
        stmt = (
            select(Enterprise, EnterpriseDevice)
            .join(EnterpriseDevice, EnterpriseDevice.enterprise_id == Enterprise.id)
            .join(DpdDevice, DpdDevice.id == EnterpriseDevice.device_id)
            .outerjoin(CorectorType, DpdDevice.corector_type_id == CorectorType.id)
            .outerjoin(Manufacturer, CorectorType.manufacturer_id == Manufacturer.id)
            .where(DpdDevice.ser_num == ser_num)
            .where(DpdDevice.ch_num == ch_num)
            .where(eff_mf == mf_dev)
            .where(eff_type == type_dev)
            .where(Enterprise.active == True)  # noqa: E712
            .order_by(EnterpriseDevice.installed_from.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        if not rows:
            return None
        if at is None:
            return rows[0][0]
        for ent, entry in rows:
            windows = device_history.resolve_windows(
                await self.get_history(ent.id)
            )
            for e, win_from, win_to in windows:
                if e.id == entry.id and device_history.covers(win_from, win_to, at):
                    return ent
        return None


def _floor_hour(stamp: Optional[datetime]) -> Optional[datetime]:
    if stamp is None:
        return None
    return stamp.replace(minute=0, second=0, microsecond=0)
