"""Carrying one branch's configuration to another installation.

Each branch runs its own HLViewer and configures it by hand — ЛУМГ, обчислювачі,
лінії з прапорцями, кільця, маршрути, лінії ДПД, підприємства з історією
коректорів. For a real branch that is thousands of rows, and the central server
that collects all branches cannot be filled in twice. This module dumps that
configuration to a JSON bundle and merges a bundle back into a database.

Three rules shape the format:

1. **No local ids travel.** Every reference is a natural key that the schema
   already protects with a unique constraint — ЛУМГ by name, an обчислювач by
   (ЛУМГ, address), a line by (ЛУМГ, address, line number), and so on. Ids are
   per-database, and `gas_volume_line`/`dpd_line`/`virtual_line` additionally
   share one Postgres sequence, so copying them across would be meaningless at
   best. As a bonus the file diffs readably between two exports.

2. **Dictionaries are not in the bundle.** `manufacturer`, `corector_type` and
   `gas_vol_calc_type` are re-seeded from `backend/db/preload_db/*.json` on
   every container start, so the central server already has them. The bundle
   only REFERENCES them, and an import that cannot resolve a reference says so
   instead of inserting into a dictionary the next restart would overwrite.

3. **Import never destroys.** Rows are created or updated; anything present
   locally and absent from the file is only listed in the report. Deleting a
   line would cascade its whole archive away, and the central server is exactly
   where that archive is supposed to accumulate.

Nothing here commits. The caller owns the transaction, which is also how the
dry run works: the import writes for real, reports exactly what happened, and
the endpoint rolls back.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.models.device_catalog_model import CorectorType, Manufacturer
from backend.db.models.dpd_line_model import DpdLine, DpdLineDevice
from backend.db.models.enterprise_model import DpdDevice, Enterprise, EnterpriseDevice
from backend.db.models.gas_route_model import GasRoute, GasRouteMember
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcType
from backend.db.models.grmu_branch_model import (
    BranchDataPath,
    GrmuBranch,
    GrmuBranchDeviceMapping,
    GrmuBranchDpdCredential,
    VirtualLine,
    VirtualLineMember,
)
from backend.db.models.line_model import Line
from backend.db.models.lumg_model import Lumg, LumgDataPath, LumgEisCode

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "hlviewer-branch-config"
BUNDLE_VERSION = 1


class BranchNotFound(LookupError):
    """No branch with that id."""


class BundleError(ValueError):
    """The uploaded file is not a branch bundle we can read."""


# ─── Report ───────────────────────────────────────────────────────────────────


@dataclass
class BranchImportReport:
    """What a merge did — or, in a dry run, what it would do.

    `errors` is the gate: non-empty means nothing was applied. `local_only` is
    deliberately a list of names rather than a count, because the whole point of
    it is that a person has to look at the entries and decide.
    """

    branch_name: str = ""
    # "chosen" — the administrator picked the target; the rest is what the file
    # itself matched: by transfer id, by name, or nothing (a new branch).
    matched_by: str = "new"  # "chosen" | "uid" | "name" | "new"
    # The branch the bundle lands on, so the screen can preselect it. None = new.
    branch_id: Optional[int] = None
    dry_run: bool = True
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    local_only: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # ЛУМГ from the file with no counterpart here, and ЛУМГ here with none in
    # the file. Shown side by side because one is usually the other renamed —
    # and left unmapped, a rename duplicates the whole calc/line tree under it.
    new_lumgs: list[str] = field(default_factory=list)
    unmatched_lumgs: list[dict] = field(default_factory=list)
    # Settings the file carries for information only — see `notes` in the bundle.
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, table: str) -> None:
        self.created[table] = self.created.get(table, 0) + 1

    def upd(self, table: str) -> None:
        self.updated[table] = self.updated.get(table, 0) + 1

    def leftover(self, table: str, name: str) -> None:
        self.local_only.setdefault(table, []).append(name)


# ─── Shared helpers ───────────────────────────────────────────────────────────


async def _scalars(session: AsyncSession, stmt) -> list:
    return list((await session.execute(stmt)).scalars().all())


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _dt(value: Optional[str], what: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BundleError(f"{what}: некоректна дата «{value}»") from exc


def _apply(obj, values: dict) -> bool:
    """Copy `values` onto `obj`, returning whether anything actually changed.

    The difference matters for the report: re-importing an unchanged file must
    say "0 updated", not repeat the whole tree back at the administrator.
    """
    changed = False
    for key, value in values.items():
        if getattr(obj, key) != value:
            setattr(obj, key, value)
            changed = True
    return changed


def _physical_ref(lumg_name: str, address: int, line_no: int) -> dict:
    return {"kind": "physical", "lumg": lumg_name, "address": address, "line": line_no}


def _dpd_ref(name: str) -> dict:
    return {"kind": "dpd", "name": name}


def _as_int(value) -> Optional[int]:
    return None if value is None else int(value)


def _ref_key(ref: Optional[dict]) -> Optional[tuple]:
    """Hashable form of a line reference, for matching a member to a line."""
    if not ref:
        return None
    kind = ref.get("kind")
    if kind == "dpd":
        return ("dpd", ref.get("name"))
    if kind == "physical":
        return (
            "physical",
            ref.get("lumg"),
            _as_int(ref.get("address")),
            _as_int(ref.get("line")),
        )
    raise BundleError(f"невідомий тип посилання на лінію: {kind!r}")


def _ref_label(ref: Optional[dict]) -> str:
    if not ref:
        return "—"
    if ref.get("kind") == "dpd":
        return f"ДПД «{ref.get('name')}»"
    return f"{ref.get('lumg')} / адреса {ref.get('address')} / лінія {ref.get('line')}"


def _corrector_label(dev: dict) -> str:
    model = dev.get("model_name") or (
        f"mf_dev={dev.get('mf_dev')} type_dev={dev.get('type_dev')}"
    )
    return f"№{dev.get('ser_num')} {model} кан.{dev.get('ch_num')}"


def _path_payload(row) -> Optional[dict]:
    return None if row is None else {"path": row.path, "active": row.active}


def _credential_payload(cred, include_secrets: bool) -> Optional[dict]:
    if cred is None:
        return None
    payload = {
        "username": cred.username,
        "api_base_url": cred.api_base_url,
        "auth_url": cred.auth_url,
        "timeout_sec": cred.timeout_sec,
    }
    if include_secrets:
        payload["password"] = cred.password
    return payload


def _device_payload(dev: DpdDevice, ct_info: dict) -> dict:
    """A corrector as a catalog reference, falling back to raw DPD codes.

    `dpd_device.corector_type_id` is nullable — rows the catalog backfill could
    not match still address the API by (mf_dev, type_dev). Those keep travelling
    on their own codes; everything else travels as (виробник, модель), which is
    what survives differing catalog ids across installations.
    """
    info = ct_info.get(dev.corector_type_id) if dev.corector_type_id else None
    if info:
        return {
            "ser_num": dev.ser_num,
            "ch_num": dev.ch_num,
            "mf_dev": info["mf_dev"],
            "model_name": info["model_name"],
            "type_dev": info["type_dev"],
        }
    return {
        "ser_num": dev.ser_num,
        "ch_num": dev.ch_num,
        "mf_dev": dev.mf_dev,
        "model_name": None,
        "type_dev": dev.type_dev,
    }


async def _catalog_by_id(session: AsyncSession) -> dict[int, dict]:
    rows = (
        await session.execute(
            select(CorectorType, Manufacturer).join(
                Manufacturer, CorectorType.manufacturer_id == Manufacturer.id
            )
        )
    ).all()
    return {
        ct.id: {"mf_dev": mfr.mf_dev, "model_name": ct.model_name, "type_dev": ct.type_dev}
        for ct, mfr in rows
    }


async def _catalog_by_name(session: AsyncSession) -> dict[tuple, int]:
    """(mf_dev, model_name) → corector_type.id — the key that crosses installs."""
    rows = (
        await session.execute(
            select(CorectorType, Manufacturer).join(
                Manufacturer, CorectorType.manufacturer_id == Manufacturer.id
            )
        )
    ).all()
    return {(mfr.mf_dev, ct.model_name): ct.id for ct, mfr in rows}


# ─── Export ───────────────────────────────────────────────────────────────────


async def export_branch(
    session: AsyncSession,
    branch_id: int,
    include_secrets: bool = True,
) -> dict:
    """Build the bundle for one branch. Reads only."""
    branch = await session.get(GrmuBranch, branch_id)
    if branch is None:
        raise BranchNotFound(f"branch {branch_id}")

    ct_info = await _catalog_by_id(session)
    type_code = {
        t.id: t.type_id for t in await _scalars(session, select(GasVolumeCalcType))
    }

    # ── ЛУМГ → обчислювачі → лінії ────────────────────────────────────────────
    lumgs = await _scalars(
        session, select(Lumg).where(Lumg.branch_id == branch_id).order_by(Lumg.name)
    )
    lumg_ids = [l.id for l in lumgs] or [-1]
    lumg_name = {l.id: l.name for l in lumgs}

    lumg_paths = {
        p.lumg_id: p
        for p in await _scalars(
            session, select(LumgDataPath).where(LumgDataPath.lumg_id.in_(lumg_ids))
        )
    }
    eis_codes: dict[int, list[str]] = defaultdict(list)
    for row in await _scalars(
        session,
        select(LumgEisCode)
        .where(LumgEisCode.lumg_id.in_(lumg_ids))
        .order_by(LumgEisCode.eis_code),
    ):
        eis_codes[row.lumg_id].append(row.eis_code)

    calcs = await _scalars(
        session,
        select(GasVolumeCalc)
        .where(GasVolumeCalc.lumg_id.in_(lumg_ids))
        .order_by(GasVolumeCalc.lumg_id, GasVolumeCalc.address),
    )
    calc_by_id = {c.id: c for c in calcs}
    lines = await _scalars(
        session,
        select(Line)
        .where(Line.gas_volume_calc_id.in_(list(calc_by_id) or [-1]))
        .order_by(Line.gas_volume_calc_id, Line.line),
    )

    lines_by_calc: dict[int, list[Line]] = defaultdict(list)
    # Physical and DPD lines get separate maps rather than one keyed by id.
    # In production `shared_line_id_seq` keeps the two id spaces apart, but a
    # schema built straight from the models (which is what the tests do) has a
    # sequence per table — and then a ring member would resolve to the wrong
    # kind of line. The kind is known at every call site anyway.
    phys_ref: dict[int, dict] = {}
    for line in lines:
        calc = calc_by_id[line.gas_volume_calc_id]
        lines_by_calc[calc.id].append(line)
        phys_ref[line.id] = _physical_ref(lumg_name[calc.lumg_id], calc.address, line.line)

    calcs_by_lumg: dict[int, list[dict]] = defaultdict(list)
    for calc in calcs:
        calcs_by_lumg[calc.lumg_id].append(
            {
                "address": calc.address,
                "name": calc.name,
                "c_time": calc.c_time,
                "type_id": type_code.get(calc.type_id),
                "lines": [
                    {
                        "line": ln.line,
                        "name": ln.name,
                        "meter": ln.meter,
                        "include_in_report": ln.include_in_report,
                        "include_in_trends": ln.include_in_trends,
                        "is_high_pressure": ln.is_high_pressure,
                        "pressure_unit": ln.pressure_unit,
                        "dp_unit": ln.dp_unit,
                    }
                    for ln in lines_by_calc[calc.id]
                ],
            }
        )

    lumgs_payload = [
        {
            "name": l.name,
            "data_path": _path_payload(lumg_paths.get(l.id)),
            "eis_codes": eis_codes.get(l.id, []),
            "calcs": calcs_by_lumg.get(l.id, []),
        }
        for l in lumgs
    ]

    # ── Лінії ДПД ─────────────────────────────────────────────────────────────
    dpd_lines = await _scalars(
        session,
        select(DpdLine).where(DpdLine.branch_id == branch_id).order_by(DpdLine.name),
    )
    dpd_ref_by_id = {dl.id: _dpd_ref(dl.name) for dl in dpd_lines}

    dpd_devices: dict[int, list[dict]] = defaultdict(list)
    for dev in await _scalars(
        session,
        select(DpdLineDevice)
        .where(DpdLineDevice.dpd_line_id.in_([d.id for d in dpd_lines] or [-1]))
        .order_by(DpdLineDevice.dpd_line_id, DpdLineDevice.installed_from),
    ):
        info = ct_info.get(dev.corector_type_id, {})
        dpd_devices[dev.dpd_line_id].append(
            {
                "ser_num": dev.ser_num,
                "ch_num": dev.ch_num,
                "mf_dev": info.get("mf_dev"),
                "model_name": info.get("model_name"),
                "type_dev": info.get("type_dev"),
                "installed_from": _iso(dev.installed_from),
            }
        )

    dpd_payload = [
        {
            "name": dl.name,
            "lumg": lumg_name.get(dl.lumg_id),
            "description": dl.description,
            "active": dl.active,
            "include_in_trends": dl.include_in_trends,
            "include_in_report": dl.include_in_report,
            "devices": dpd_devices.get(dl.id, []),
        }
        for dl in dpd_lines
    ]

    # ── Кільця ────────────────────────────────────────────────────────────────
    rings = await _scalars(
        session,
        select(VirtualLine)
        .where(VirtualLine.branch_id == branch_id)
        .order_by(VirtualLine.name),
    )
    ring_members: dict[int, list[dict]] = defaultdict(list)
    for m in await _scalars(
        session,
        select(VirtualLineMember)
        .where(VirtualLineMember.virtual_line_id.in_([r.id for r in rings] or [-1]))
        .order_by(VirtualLineMember.virtual_line_id, VirtualLineMember.sort_order),
    ):
        ref = (
            phys_ref.get(m.line_id)
            if m.line_id is not None
            else dpd_ref_by_id.get(m.dpd_line_id)
        )
        if ref is None:
            continue
        ring_members[m.virtual_line_id].append({"ref": ref, "sort_order": m.sort_order})

    rings_payload = [
        {
            "name": r.name,
            "lumg": lumg_name.get(r.lumg_id),
            "description": r.description,
            "active": r.active,
            "include_in_trends": r.include_in_trends,
            "members": ring_members.get(r.id, []),
        }
        for r in rings
    ]

    # ── Маршрути ФХП ──────────────────────────────────────────────────────────
    routes = await _scalars(
        session,
        select(GasRoute).where(GasRoute.branch_id == branch_id).order_by(GasRoute.number),
    )
    route_members: dict[int, list[dict]] = defaultdict(list)
    for m in await _scalars(
        session,
        select(GasRouteMember)
        .where(GasRouteMember.route_id.in_([r.id for r in routes] or [-1]))
        .order_by(GasRouteMember.route_id, GasRouteMember.sort_order),
    ):
        ref = phys_ref.get(m.line_id)
        if ref is None:
            continue
        route_members[m.route_id].append(
            {"ref": ref, "is_reference": m.is_reference, "sort_order": m.sort_order}
        )

    routes_payload = [
        {
            "number": r.number,
            "name": r.name,
            "description": r.description,
            "active": r.active,
            "members": route_members.get(r.id, []),
        }
        for r in routes
    ]

    # ── Підприємства ──────────────────────────────────────────────────────────
    enterprises = await _scalars(
        session,
        select(Enterprise)
        .where(Enterprise.branch_id == branch_id)
        .order_by(Enterprise.enterprise_name),
    )
    ent_devices: dict[int, list[dict]] = defaultdict(list)
    ent_ids = [e.id for e in enterprises]
    if ent_ids:
        rows = (
            await session.execute(
                select(EnterpriseDevice, DpdDevice)
                .join(DpdDevice, EnterpriseDevice.device_id == DpdDevice.id)
                .where(EnterpriseDevice.enterprise_id.in_(ent_ids))
                .order_by(EnterpriseDevice.enterprise_id, EnterpriseDevice.installed_from)
            )
        ).all()
        for link, dev in rows:
            payload = _device_payload(dev, ct_info)
            payload["installed_from"] = _iso(link.installed_from)
            payload["removed_at"] = _iso(link.removed_at)
            ent_devices[link.enterprise_id].append(payload)

    enterprises_payload = [
        {
            "name": e.enterprise_name,
            "active": e.active,
            "enabled": e.enabled,
            "ref": (
                phys_ref.get(e.line_id)
                if e.line_id is not None
                else dpd_ref_by_id.get(e.dpd_line_id)
            ),
            "devices": ent_devices.get(e.id, []),
        }
        for e in enterprises
    ]

    # ── Легасі-маппінг приладів ───────────────────────────────────────────────
    mappings = await _scalars(
        session,
        select(GrmuBranchDeviceMapping)
        .where(GrmuBranchDeviceMapping.branch_id == branch_id)
        .order_by(GrmuBranchDeviceMapping.ser_num),
    )
    mappings_payload = [
        {
            "ser_num": m.ser_num,
            "mf_dev": m.mf_dev,
            "type_dev": m.type_dev,
            "ch_num": m.ch_num,
            "ref": phys_ref.get(m.line_id),
            "grmu_branch_name": m.grmu_branch_name,
            "counterpart": m.counterpart,
            "sector": m.sector,
            "status": m.status,
            "status_changed_at": _iso(m.status_changed_at),
            "device_type": m.device_type,
            "manufacturer": m.manufacturer,
            "active": m.active,
        }
        for m in mappings
    ]

    # ── Філія ─────────────────────────────────────────────────────────────────
    data_path = (
        await session.execute(
            select(BranchDataPath).where(BranchDataPath.branch_id == branch_id)
        )
    ).scalars().first()
    cred = (
        await session.execute(
            select(GrmuBranchDpdCredential).where(
                GrmuBranchDpdCredential.branch_id == branch_id
            )
        )
    ).scalars().first()

    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "includes_secrets": bool(include_secrets and cred is not None),
        "branch": {
            "uid": str(branch.export_uid or uuid.uuid4()),
            "name": branch.name,
            "short_name": branch.short_name,
            "region": branch.region,
            "active": branch.active,
            "data_path": _path_payload(data_path),
            "dpd_credential": _credential_payload(cred, include_secrets),
        },
        "lumgs": lumgs_payload,
        "dpd_lines": dpd_payload,
        "virtual_lines": rings_payload,
        "gas_routes": routes_payload,
        "enterprises": enterprises_payload,
        "device_mappings": mappings_payload,
    }


# ─── Import ───────────────────────────────────────────────────────────────────


def _check_envelope(bundle: Any) -> None:
    if not isinstance(bundle, dict):
        raise BundleError("Файл не є об'єктом JSON")
    if bundle.get("format") != BUNDLE_FORMAT:
        raise BundleError(
            "Це не файл конфігурації філії HLViewer "
            f"(очікується format={BUNDLE_FORMAT!r})"
        )
    version = bundle.get("version")
    if version != BUNDLE_VERSION:
        raise BundleError(
            f"Версія формату {version!r} не підтримується — очікується {BUNDLE_VERSION}"
        )
    if not isinstance(bundle.get("branch"), dict):
        raise BundleError("У файлі немає секції «branch»")
    if not bundle["branch"].get("name"):
        raise BundleError("У файлі не вказана назва філії")


def _bundle_uid(bundle: dict) -> Optional[uuid.UUID]:
    raw = bundle["branch"].get("uid")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise BundleError(f"Некоректний ідентифікатор філії «{raw}»") from exc


def _resolve_corrector(
    dev: dict, ct_by_name: dict[tuple, int], *, model_required: bool
) -> tuple[Optional[int], Optional[str]]:
    """(corector_type_id, error).

    `model_required` is the difference between the two device histories:
    `dpd_line_device.corector_type_id` is NOT NULL, while a `dpd_device` may
    still be addressed by raw (mf_dev, type_dev) — those are the rows the
    catalog backfill could not match, and they must survive the trip.
    """
    mf_dev = dev.get("mf_dev")
    model = dev.get("model_name")
    if model and mf_dev is not None:
        ct_id = ct_by_name.get((int(mf_dev), model))
        if ct_id is None:
            return None, (
                f"моделі «{model}» виробника з кодом mf_dev={mf_dev} немає в довіднику "
                "коректорів цього сервера"
            )
        return ct_id, None
    if model_required:
        return None, "коректор без моделі — для лінії ДПД модель обов'язкова"
    if mf_dev is None or dev.get("type_dev") is None:
        return None, "коректор без моделі й без кодів mf_dev/type_dev"
    return None, None


async def import_branch(
    session: AsyncSession,
    bundle: Any,
    dry_run: bool = True,
    *,
    target_branch_id: Optional[int] = None,
    create_new: bool = False,
    lumg_map: Optional[dict[str, int]] = None,
) -> BranchImportReport:
    """Merge a bundle into this database.

    Writes through `session` but never commits: the caller commits an apply and
    rolls a dry run back. That is what makes the preview exact — it is the real
    import, counted and then undone, so a constraint the file would violate is
    reported before anyone presses «Застосувати» rather than after.

    `target_branch_id` / `create_new` / `lumg_map` are the administrator's
    answer to the one question the file cannot answer: which existing rows this
    configuration IS. Left out, the bundle matches itself by transfer id and
    then by name — right whenever the branch was transferred before, wrong the
    first time it arrives under a name that has since changed here.
    """
    report = BranchImportReport(dry_run=dry_run)
    try:
        _check_envelope(bundle)
        uid = _bundle_uid(bundle)
    except BundleError as exc:
        report.errors.append(str(exc))
        return report

    report.branch_name = bundle["branch"]["name"]
    report.notes = bundle.get("notes") or {}

    try:
        ctx = await _validate(
            session, bundle, uid, report,
            target_branch_id=target_branch_id,
            create_new=create_new,
            lumg_map=lumg_map or {},
        )
        if report.errors:
            return report
        await _write(session, bundle, uid, ctx, report)
    except BundleError as exc:
        report.errors.append(str(exc))
    return report


async def _validate(
    session: AsyncSession,
    bundle: dict,
    uid: Optional[uuid.UUID],
    report: BranchImportReport,
    *,
    target_branch_id: Optional[int] = None,
    create_new: bool = False,
    lumg_map: Optional[dict[str, int]] = None,
) -> dict:
    """Everything that can be refused is refused here, before a single write."""
    ct_by_name = await _catalog_by_name(session)
    type_by_code = {
        t.type_id: t.id for t in await _scalars(session, select(GasVolumeCalcType))
    }
    err = report.errors

    target = await _resolve_target(
        session, bundle, uid, target_branch_id, create_new, report
    )
    report.branch_id = target.id if target is not None else None
    lumg_targets = await _resolve_lumg_targets(
        session, bundle, target, lumg_map or {}, report
    )

    # ── ЛУМГ → обчислювачі → лінії ────────────────────────────────────────────
    defined: set[tuple] = set()
    seen_lumg: set[str] = set()
    seen_calc: set[tuple] = set()
    for lumg in bundle.get("lumgs") or []:
        name = lumg.get("name")
        if not name:
            err.append("У файлі є ЛУМГ без назви")
            continue
        if name in seen_lumg:
            err.append(f"ЛУМГ «{name}» зустрічається у файлі двічі")
        seen_lumg.add(name)
        for calc in lumg.get("calcs") or []:
            address = _as_int(calc.get("address"))
            if address is None:
                err.append(f"ЛУМГ «{name}»: обчислювач без адреси")
                continue
            if (name, address) in seen_calc:
                err.append(f"ЛУМГ «{name}»: адреса {address} зустрічається двічі")
            seen_calc.add((name, address))
            code = calc.get("type_id")
            if code is not None and int(code) not in type_by_code:
                err.append(
                    f"Обчислювач «{calc.get('name')}» (ЛУМГ «{name}»): типу з кодом "
                    f"{code} немає в довіднику цього сервера. Перенесіть FLOWTYPE.json "
                    "разом із кодом."
                )
            for line in calc.get("lines") or []:
                line_no = _as_int(line.get("line"))
                if line_no is None:
                    err.append(f"ЛУМГ «{name}», адреса {address}: лінія без номера")
                    continue
                key = ("physical", name, address, line_no)
                if key in defined:
                    err.append(
                        f"ЛУМГ «{name}», адреса {address}: лінія {line_no} двічі"
                    )
                defined.add(key)

    # ── Лінії ДПД ─────────────────────────────────────────────────────────────
    for dl in bundle.get("dpd_lines") or []:
        name = dl.get("name")
        if not name:
            err.append("У файлі є лінія ДПД без назви")
            continue
        key = ("dpd", name)
        if key in defined:
            err.append(f"Лінія ДПД «{name}» зустрічається у файлі двічі")
        defined.add(key)
        if dl.get("lumg") and dl["lumg"] not in seen_lumg:
            err.append(f"Лінія ДПД «{name}» посилається на невідомий ЛУМГ «{dl['lumg']}»")
        for dev in dl.get("devices") or []:
            _, problem = _resolve_corrector(dev, ct_by_name, model_required=True)
            if problem:
                err.append(f"Лінія ДПД «{name}», {_corrector_label(dev)}: {problem}")

    # ── Кільця ────────────────────────────────────────────────────────────────
    seen_ring: set[str] = set()
    for ring in bundle.get("virtual_lines") or []:
        name = ring.get("name")
        if not name:
            err.append("У файлі є кільце без назви")
            continue
        if name in seen_ring:
            err.append(f"Кільце «{name}» зустрічається у файлі двічі")
        seen_ring.add(name)
        if ring.get("lumg") and ring["lumg"] not in seen_lumg:
            err.append(f"Кільце «{name}» посилається на невідомий ЛУМГ «{ring['lumg']}»")
        for member in ring.get("members") or []:
            if _ref_key(member.get("ref")) not in defined:
                err.append(
                    f"Кільце «{name}»: у файлі немає лінії {_ref_label(member.get('ref'))}"
                )

    # ── Маршрути ФХП ──────────────────────────────────────────────────────────
    seen_route: set[str] = set()
    for route in bundle.get("gas_routes") or []:
        number = route.get("number")
        if not number:
            err.append("У файлі є маршрут без номера")
            continue
        if number in seen_route:
            err.append(f"Маршрут {number} зустрічається у файлі двічі")
        seen_route.add(number)
        for member in route.get("members") or []:
            ref = member.get("ref")
            key = _ref_key(ref)
            if key is None or key[0] != "physical":
                err.append(
                    f"Маршрут {number}: {_ref_label(ref)} — маршрут складається лише "
                    "з фізичних ліній"
                )
            elif key not in defined:
                err.append(f"Маршрут {number}: у файлі немає лінії {_ref_label(ref)}")

    # ── Підприємства ──────────────────────────────────────────────────────────
    seen_ent: set[str] = set()
    for ent in bundle.get("enterprises") or []:
        name = ent.get("name")
        if not name:
            err.append("У файлі є підприємство без назви")
            continue
        if name in seen_ent:
            err.append(
                f"Підприємство «{name}» зустрічається у файлі двічі — назва в межах "
                "філії має бути унікальною, інакше неможливо сказати, який рядок "
                "оновлювати"
            )
        seen_ent.add(name)
        ref = ent.get("ref")
        if ref is not None and _ref_key(ref) not in defined:
            err.append(f"Підприємство «{name}»: у файлі немає лінії {_ref_label(ref)}")
        for dev in ent.get("devices") or []:
            _, problem = _resolve_corrector(dev, ct_by_name, model_required=False)
            if problem:
                err.append(f"Підприємство «{name}», {_corrector_label(dev)}: {problem}")

    return {
        "ct_by_name": ct_by_name,
        "type_by_code": type_by_code,
        "target": target,
        "lumg_targets": lumg_targets,
    }


async def _resolve_target(
    session: AsyncSession,
    bundle: dict,
    uid: Optional[uuid.UUID],
    target_branch_id: Optional[int],
    create_new: bool,
    report: BranchImportReport,
) -> Optional[GrmuBranch]:
    """Which branch row this bundle lands on. None = a new one.

    An explicit choice wins over anything the file says about itself; the
    automatic path stays for the ordinary repeat transfer, where the transfer id
    already matches and nothing has to be decided.
    """
    name = bundle["branch"]["name"]
    by_uid = None
    if uid is not None:
        by_uid = (
            await session.execute(select(GrmuBranch).where(GrmuBranch.export_uid == uid))
        ).scalars().first()
    by_name = (
        await session.execute(select(GrmuBranch).where(GrmuBranch.name == name))
    ).scalars().first()

    if target_branch_id is not None and create_new:
        report.errors.append(
            "Обрано і наявну філію, і створення нової — оберіть щось одне."
        )
        return None

    if target_branch_id is not None:
        report.matched_by = "chosen"
        chosen = await session.get(GrmuBranch, target_branch_id)
        if chosen is None:
            report.errors.append(f"Обраної філії (id={target_branch_id}) не існує.")
            return None
        # Two branches sharing one transfer id would make every later import
        # ambiguous, so a bundle can only be pointed at the branch it already
        # belongs to — or at one that has never taken part in a transfer.
        if by_uid is not None and by_uid.id != chosen.id:
            report.errors.append(
                f"Цей файл вже переносився у філію «{by_uid.name}». Оберіть її або "
                "створіть нову."
            )
        if by_name is not None and by_name.id != chosen.id:
            report.errors.append(
                f"Назву «{name}» вже носить філія «{by_name.name}» (id={by_name.id}) — "
                f"обрану філію не можна на неї перейменувати."
            )
        if chosen.name != name:
            report.warnings.append(
                f"Філію «{chosen.name}» буде перейменовано на «{name}»."
            )
        return chosen

    if create_new:
        report.matched_by = "new"
        if by_uid is not None:
            report.errors.append(
                f"Цей файл вже переносився у філію «{by_uid.name}» — створення другої "
                "копії дало б дві філії з одним ідентифікатором перенесення."
            )
        if by_name is not None:
            report.errors.append(
                f"Філія з назвою «{name}» вже існує — оберіть її або перейменуйте."
            )
        return None

    if by_uid is not None:
        report.matched_by = "uid"
        if by_name is not None and by_name.id != by_uid.id:
            report.errors.append(
                f"Назву «{name}» вже носить інша філія (id={by_name.id}), "
                f"а цей файл належить філії «{by_uid.name}». Перейменуйте одну з них."
            )
        if by_uid.name != name:
            report.warnings.append(
                f"Філію «{by_uid.name}» буде перейменовано на «{name}»."
            )
        return by_uid

    if by_name is not None:
        report.matched_by = "name"
        report.warnings.append(
            f"Філію знайдено за назвою «{by_name.name}» — її дані буде оновлено, "
            "а ідентифікатор перенесення взято з файлу. Якщо це не та філія, "
            "оберіть іншу зі списку."
        )
        return by_name

    report.matched_by = "new"
    return None


async def _resolve_lumg_targets(
    session: AsyncSession,
    bundle: dict,
    target: Optional[GrmuBranch],
    lumg_map: dict[str, int],
    report: BranchImportReport,
) -> dict[str, Optional[Lumg]]:
    """File ЛУМГ name → the row it updates, or None to create it.

    Unmapped names fall back to matching by name, which is right until someone
    renames a ЛУМГ at the branch: then the file's name matches nothing, a second
    ЛУМГ appears, and every обчислювач and лінія under it is recreated while the
    originals keep the archive. That is why the two unmatched lists go into the
    report — so the screen can offer the mapping instead of the duplication.
    """
    existing = (
        []
        if target is None
        else await _scalars(session, select(Lumg).where(Lumg.branch_id == target.id))
    )
    by_name = {row.name: row for row in existing}
    by_id = {row.id: row for row in existing}
    file_names = [
        item["name"] for item in bundle.get("lumgs") or [] if item.get("name")
    ]

    out: dict[str, Optional[Lumg]] = {}
    claimed: dict[int, str] = {}

    # Explicit mappings first: they have priority over a same-name row, and a
    # by-name match must not quietly steal a row that a mapping already took.
    for name in file_names:
        raw = lumg_map.get(name)
        if raw is None:
            continue
        row = by_id.get(int(raw))
        if row is None:
            report.errors.append(
                f"ЛУМГ, обраний для «{name}», не належить цій філії."
            )
            continue
        if row.id in claimed:
            report.errors.append(
                f"ЛУМГ «{row.name}» зіставлено одразу з «{claimed[row.id]}» і «{name}»."
            )
            continue
        clash = by_name.get(name)
        if clash is not None and clash.id != row.id:
            report.errors.append(
                f"Неможливо перейменувати «{row.name}» на «{name}»: цю назву вже "
                "носить інший ЛУМГ цієї філії."
            )
            continue
        claimed[row.id] = name
        out[name] = row

    for name in file_names:
        if name in out:
            continue
        row = by_name.get(name)
        if row is not None and row.id in claimed:
            report.errors.append(
                f"ЛУМГ «{row.name}» зіставлено з «{claimed[row.id]}», тож «{name}» "
                "не може оновити той самий рядок."
            )
            continue
        out[name] = row
        if row is None:
            report.new_lumgs.append(name)

    report.unmatched_lumgs = [
        {"id": row.id, "name": row.name}
        for row in existing
        if row.id not in claimed and row.name not in out
    ]
    if report.new_lumgs and report.unmatched_lumgs:
        report.warnings.append(
            "У файлі є ЛУМГ, яких тут немає: "
            + ", ".join(f"«{n}»" for n in report.new_lumgs)
            + ". Їх буде створено разом з усіма обчислювачами й лініями. Якщо це "
            "перейменування — зіставте їх із наявними ("
            + ", ".join(f"«{u['name']}»" for u in report.unmatched_lumgs)
            + "), інакше дерево подвоїться."
        )
    return out


async def _write(
    session: AsyncSession,
    bundle: dict,
    uid: Optional[uuid.UUID],
    ctx: dict,
    report: BranchImportReport,
) -> None:
    ct_by_name: dict[tuple, int] = ctx["ct_by_name"]
    type_by_code: dict[int, int] = ctx["type_by_code"]

    branch = await _write_branch(session, bundle["branch"], uid, ctx["target"], report)
    await session.flush()
    bid = branch.id
    report.branch_id = bid

    ref_to_id: dict[tuple, int] = {}
    lumg_ids = await _write_lumgs(
        session, bundle, bid, type_by_code, ctx["lumg_targets"], ref_to_id, report
    )
    await _write_dpd_lines(session, bundle, bid, lumg_ids, ct_by_name, ref_to_id, report)
    await _write_rings(session, bundle, bid, lumg_ids, ref_to_id, report)
    await _write_routes(session, bundle, bid, ref_to_id, report)
    await _write_enterprises(session, bundle, bid, ct_by_name, ref_to_id, report)
    await _write_device_mappings(session, bundle, bid, ref_to_id, report)
    await session.flush()

    if report.created.get("branch_data_path") or report.created.get("lumg_data_path"):
        report.warnings.append(
            "Нові шляхи до даних заведено неактивними — цей сервер не бачить диск філії. "
            "Якщо шлях мережевий і доступний, увімкніть його у «Шляхи до даних»."
        )


async def _write_branch(
    session: AsyncSession,
    data: dict,
    uid: Optional[uuid.UUID],
    branch: Optional[GrmuBranch],
    report: BranchImportReport,
) -> GrmuBranch:
    """`branch` is what _resolve_target decided on; None means create."""
    values = {
        "name": data["name"],
        "short_name": data.get("short_name"),
        "region": data.get("region"),
        "active": bool(data.get("active", True)),
    }
    if branch is None:
        branch = GrmuBranch(**values, export_uid=uid or uuid.uuid4())
        session.add(branch)
        report.add("grmu_branch")
    else:
        if uid is not None:
            values["export_uid"] = uid
        if _apply(branch, values):
            report.upd("grmu_branch")
    await session.flush()

    await _write_branch_path(session, data.get("data_path"), branch.id, report)
    await _write_credential(session, data.get("dpd_credential"), branch.id, report)
    return branch


async def _write_branch_path(
    session: AsyncSession, payload: Optional[dict], bid: int, report: BranchImportReport
) -> None:
    if not payload:
        return
    row = (
        await session.execute(select(BranchDataPath).where(BranchDataPath.branch_id == bid))
    ).scalars().first()
    if row is None:
        # A path this server has never seen starts inactive: the file poller
        # walks active paths every two minutes, and the branch's disk is not
        # this server's. Whether it is reachable is a local decision, so an
        # existing row keeps its own `active` — otherwise re-importing a bundle
        # on the branch that produced it would switch off its own polling.
        session.add(BranchDataPath(branch_id=bid, path=payload["path"], active=False))
        report.add("branch_data_path")
    elif _apply(row, {"path": payload["path"]}):
        report.upd("branch_data_path")


async def _write_credential(
    session: AsyncSession, payload: Optional[dict], bid: int, report: BranchImportReport
) -> None:
    if not payload:
        return
    row = (
        await session.execute(
            select(GrmuBranchDpdCredential).where(GrmuBranchDpdCredential.branch_id == bid)
        )
    ).scalars().first()
    values = {
        "username": payload.get("username") or "",
        "api_base_url": payload.get("api_base_url"),
        "auth_url": payload.get("auth_url"),
        "timeout_sec": int(payload.get("timeout_sec") or 30),
    }
    password = payload.get("password")
    if password is not None:
        values["password"] = password
    if row is None:
        if password is None:
            report.warnings.append(
                "Пароль ДПД у файлі відсутній — доступ створено з порожнім паролем, "
                "введіть його у «Доступ ДПД»."
            )
        session.add(
            GrmuBranchDpdCredential(branch_id=bid, password=password or "", **{
                k: v for k, v in values.items() if k != "password"
            })
        )
        report.add("grmu_branch_dpd_credential")
    else:
        if password is None:
            report.warnings.append(
                "Пароль ДПД у файлі відсутній — наявний пароль залишено без змін."
            )
        if _apply(row, values):
            report.upd("grmu_branch_dpd_credential")


async def _write_lumgs(
    session: AsyncSession,
    bundle: dict,
    bid: int,
    type_by_code: dict[int, int],
    lumg_targets: dict[str, Optional[Lumg]],
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> dict[str, int]:
    existing = await _scalars(session, select(Lumg).where(Lumg.branch_id == bid))
    payload = bundle.get("lumgs") or []
    rows: dict[str, Lumg] = {}
    for item in payload:
        name = item["name"]
        row = lumg_targets.get(name)
        if row is None:
            row = Lumg(branch_id=bid, name=name)
            session.add(row)
            report.add("lumg")
        elif _apply(row, {"name": name}):
            # A mapped ЛУМГ under a new name — the rename travelling across.
            report.upd("lumg")
        rows[name] = row
    await session.flush()
    lumg_ids = {name: row.id for name, row in rows.items()}

    touched = {row.id for row in rows.values()}
    for row in existing:
        if row.id not in touched:
            report.leftover("lumg", row.name)

    await _write_lumg_paths(session, payload, lumg_ids, report)
    await _write_eis_codes(session, payload, lumg_ids, report)
    await _write_calcs(session, payload, lumg_ids, type_by_code, ref_to_id, report)
    return lumg_ids


async def _write_lumg_paths(
    session: AsyncSession,
    payload: list[dict],
    lumg_ids: dict[str, int],
    report: BranchImportReport,
) -> None:
    ids = list(lumg_ids.values()) or [-1]
    existing = {
        row.lumg_id: row
        for row in await _scalars(
            session, select(LumgDataPath).where(LumgDataPath.lumg_id.in_(ids))
        )
    }
    for item in payload:
        path = item.get("data_path")
        if not path:
            continue
        lid = lumg_ids[item["name"]]
        row = existing.get(lid)
        # Same rule as the branch path: new arrives inactive, existing keeps the
        # local decision. See _write_branch_path.
        if row is None:
            session.add(LumgDataPath(lumg_id=lid, path=path["path"], active=False))
            report.add("lumg_data_path")
        elif _apply(row, {"path": path["path"]}):
            report.upd("lumg_data_path")


async def _write_eis_codes(
    session: AsyncSession,
    payload: list[dict],
    lumg_ids: dict[str, int],
    report: BranchImportReport,
) -> None:
    # eis_code is unique across the WHOLE database, not per branch — on a server
    # holding several branches two of them can genuinely claim the same code.
    # Skipping with a warning is the only honest answer; inserting would abort
    # the whole import over one dictionary row.
    owner = {
        row.eis_code: row.lumg_id
        for row in await _scalars(session, select(LumgEisCode))
    }
    for item in payload:
        lid = lumg_ids[item["name"]]
        for code in item.get("eis_codes") or []:
            holder = owner.get(code)
            if holder == lid:
                continue
            if holder is not None:
                report.warnings.append(
                    f"ЄІС-код {code} вже закріплений за іншим ЛУМГ — пропущено."
                )
                continue
            session.add(LumgEisCode(lumg_id=lid, eis_code=code))
            owner[code] = lid
            report.add("lumg_eis_code")


async def _write_calcs(
    session: AsyncSession,
    payload: list[dict],
    lumg_ids: dict[str, int],
    type_by_code: dict[int, int],
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> None:
    ids = list(lumg_ids.values()) or [-1]
    existing = {
        (row.lumg_id, row.address): row
        for row in await _scalars(
            session, select(GasVolumeCalc).where(GasVolumeCalc.lumg_id.in_(ids))
        )
    }
    wanted: dict[tuple, GasVolumeCalc] = {}
    for item in payload:
        lid = lumg_ids[item["name"]]
        for calc in item.get("calcs") or []:
            address = int(calc["address"])
            code = calc.get("type_id")
            values = {
                "name": calc.get("name") or "",
                "c_time": int(calc.get("c_time") or 7),
                "type_id": type_by_code.get(int(code)) if code is not None else None,
            }
            row = existing.get((lid, address))
            if row is None:
                row = GasVolumeCalc(lumg_id=lid, address=address, **values)
                session.add(row)
                report.add("gas_volume_calc")
            elif _apply(row, values):
                report.upd("gas_volume_calc")
            wanted[(item["name"], address)] = row
    await session.flush()

    name_of = {lid: name for name, lid in lumg_ids.items()}
    for (lid, address), row in existing.items():
        if (name_of.get(lid), address) not in wanted:
            report.leftover("gas_volume_calc", f"{name_of.get(lid)} / {address}")

    await _write_lines(session, payload, wanted, ref_to_id, report)


async def _write_lines(
    session: AsyncSession,
    payload: list[dict],
    calcs: dict[tuple, GasVolumeCalc],
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> None:
    calc_ids = [c.id for c in calcs.values()] or [-1]
    existing = {
        (row.gas_volume_calc_id, row.line): row
        for row in await _scalars(
            session, select(Line).where(Line.gas_volume_calc_id.in_(calc_ids))
        )
    }
    wanted: dict[tuple, Line] = {}
    for item in payload:
        lumg_name = item["name"]
        for calc in item.get("calcs") or []:
            address = int(calc["address"])
            parent = calcs[(lumg_name, address)]
            for line in calc.get("lines") or []:
                line_no = int(line["line"])
                values = {
                    "name": line.get("name") or "",
                    "meter": bool(line.get("meter", False)),
                    "include_in_report": bool(line.get("include_in_report", False)),
                    "include_in_trends": bool(line.get("include_in_trends", False)),
                    "is_high_pressure": bool(line.get("is_high_pressure", False)),
                    "pressure_unit": line.get("pressure_unit") or "кгс/см²",
                    "dp_unit": line.get("dp_unit") or "кгс/м²",
                }
                row = existing.get((parent.id, line_no))
                if row is None:
                    row = Line(gas_volume_calc_id=parent.id, line=line_no, **values)
                    session.add(row)
                    report.add("gas_volume_line")
                elif _apply(row, values):
                    report.upd("gas_volume_line")
                wanted[(parent.id, line_no)] = row
    await session.flush()

    for key, row in existing.items():
        if key not in wanted:
            report.leftover("gas_volume_line", row.name or f"№{row.line}")

    calc_key = {c.id: key for key, c in calcs.items()}
    for (calc_id, line_no), row in wanted.items():
        lumg_name, address = calc_key[calc_id]
        ref_to_id[("physical", lumg_name, address, line_no)] = row.id


async def _write_dpd_lines(
    session: AsyncSession,
    bundle: dict,
    bid: int,
    lumg_ids: dict[str, int],
    ct_by_name: dict[tuple, int],
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> None:
    existing = {
        row.name: row
        for row in await _scalars(session, select(DpdLine).where(DpdLine.branch_id == bid))
    }
    wanted: dict[str, DpdLine] = {}
    payload = bundle.get("dpd_lines") or []
    for item in payload:
        name = item["name"]
        values = {
            "lumg_id": lumg_ids.get(item.get("lumg")) if item.get("lumg") else None,
            "description": item.get("description"),
            "active": bool(item.get("active", True)),
            "include_in_trends": bool(item.get("include_in_trends", False)),
            "include_in_report": bool(item.get("include_in_report", False)),
        }
        row = existing.get(name)
        if row is None:
            row = DpdLine(branch_id=bid, name=name, **values)
            session.add(row)
            report.add("dpd_line")
        elif _apply(row, values):
            report.upd("dpd_line")
        wanted[name] = row
    await session.flush()

    for name in existing:
        if name not in wanted:
            report.leftover("dpd_line", name)
    for name, row in wanted.items():
        ref_to_id[("dpd", name)] = row.id

    line_ids = [r.id for r in wanted.values()] or [-1]
    devices = {
        (row.dpd_line_id, row.installed_from): row
        for row in await _scalars(
            session, select(DpdLineDevice).where(DpdLineDevice.dpd_line_id.in_(line_ids))
        )
    }
    seen: set[tuple] = set()
    for item in payload:
        parent = wanted[item["name"]]
        for dev in item.get("devices") or []:
            ct_id, _ = _resolve_corrector(dev, ct_by_name, model_required=True)
            installed = _dt(dev.get("installed_from"), f"Лінія ДПД «{item['name']}»")
            key = (parent.id, installed)
            seen.add(key)
            values = {
                "ser_num": int(dev["ser_num"]),
                "corector_type_id": ct_id,
                "ch_num": int(dev.get("ch_num") or 0),
            }
            row = devices.get(key)
            if row is None:
                session.add(
                    DpdLineDevice(dpd_line_id=parent.id, installed_from=installed, **values)
                )
                report.add("dpd_line_device")
            elif _apply(row, values):
                report.upd("dpd_line_device")
    for key in devices:
        if key not in seen:
            report.warnings.append(
                "Історія лінії ДПД містить запис, якого немає у файлі "
                f"(встановлено {key[1]}) — його залишено, перевірте вікна коректорів."
            )


async def _write_rings(
    session: AsyncSession,
    bundle: dict,
    bid: int,
    lumg_ids: dict[str, int],
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> None:
    existing = {
        row.name: row
        for row in await _scalars(
            session, select(VirtualLine).where(VirtualLine.branch_id == bid)
        )
    }
    wanted: dict[str, VirtualLine] = {}
    payload = bundle.get("virtual_lines") or []
    for item in payload:
        name = item["name"]
        values = {
            "lumg_id": lumg_ids.get(item.get("lumg")) if item.get("lumg") else None,
            "description": item.get("description"),
            "active": bool(item.get("active", True)),
            "include_in_trends": bool(item.get("include_in_trends", False)),
        }
        row = existing.get(name)
        if row is None:
            row = VirtualLine(branch_id=bid, name=name, **values)
            session.add(row)
            report.add("virtual_line")
        elif _apply(row, values):
            report.upd("virtual_line")
        wanted[name] = row
    await session.flush()

    for name in existing:
        if name not in wanted:
            report.leftover("virtual_line", name)

    ring_ids = [r.id for r in wanted.values()] or [-1]
    # The member key carries the KIND as well as the id: a physical and a DPD
    # line only have distinct ids because of shared_line_id_seq, and a schema
    # built from the models alone (the tests) has no such guarantee.
    members = {}
    for row in await _scalars(
        session,
        select(VirtualLineMember).where(VirtualLineMember.virtual_line_id.in_(ring_ids)),
    ):
        if row.line_id is not None:
            members[(row.virtual_line_id, "physical", row.line_id)] = row
        else:
            members[(row.virtual_line_id, "dpd", row.dpd_line_id)] = row
    seen: set[tuple] = set()
    for item in payload:
        parent = wanted[item["name"]]
        for member in item.get("members") or []:
            key = _ref_key(member["ref"])
            target = ref_to_id[key]
            is_dpd = key[0] == "dpd"
            seen.add((parent.id, key[0], target))
            values = {"sort_order": int(member.get("sort_order") or 0)}
            row = members.get((parent.id, key[0], target))
            if row is None:
                session.add(
                    VirtualLineMember(
                        virtual_line_id=parent.id,
                        line_id=None if is_dpd else target,
                        dpd_line_id=target if is_dpd else None,
                        **values,
                    )
                )
                report.add("virtual_line_member")
            elif _apply(row, values):
                report.upd("virtual_line_member")
    for key in members:
        if key not in seen:
            report.warnings.append(
                "У кільці на цьому сервері є лінія, якої немає у файлі — її залишено, "
                "склад кільця відрізняється від філії."
            )
            break


async def _write_routes(
    session: AsyncSession,
    bundle: dict,
    bid: int,
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> None:
    existing = {
        row.number: row
        for row in await _scalars(session, select(GasRoute).where(GasRoute.branch_id == bid))
    }
    wanted: dict[str, GasRoute] = {}
    payload = bundle.get("gas_routes") or []
    for item in payload:
        number = item["number"]
        values = {
            "name": item.get("name"),
            "description": item.get("description"),
            "active": bool(item.get("active", True)),
        }
        row = existing.get(number)
        if row is None:
            row = GasRoute(branch_id=bid, number=number, **values)
            session.add(row)
            report.add("gas_route")
        elif _apply(row, values):
            report.upd("gas_route")
        wanted[number] = row
    await session.flush()

    for number in existing:
        if number not in wanted:
            report.leftover("gas_route", number)

    # uq_gas_route_member_line is on line_id ALONE — a line belongs to at most
    # one route — so the lookup is by line, not by (route, line).
    targets = [ref_to_id[_ref_key(m["ref"])] for item in payload for m in item.get("members") or []]
    by_line = {
        row.line_id: row
        for row in await _scalars(
            session, select(GasRouteMember).where(GasRouteMember.line_id.in_(targets or [-1]))
        )
    }
    for item in payload:
        parent = wanted[item["number"]]
        for member in item.get("members") or []:
            target = ref_to_id[_ref_key(member["ref"])]
            values = {
                "route_id": parent.id,
                "is_reference": bool(member.get("is_reference", False)),
                "sort_order": int(member.get("sort_order") or 0),
            }
            row = by_line.get(target)
            if row is None:
                session.add(GasRouteMember(line_id=target, **values))
                report.add("gas_route_member")
            else:
                if row.route_id != parent.id:
                    report.warnings.append(
                        f"Лінія {_ref_label(member['ref'])} переведена до маршруту "
                        f"{item['number']} — лінія може належати лише одному маршруту."
                    )
                if _apply(row, values):
                    report.upd("gas_route_member")


async def _write_enterprises(
    session: AsyncSession,
    bundle: dict,
    bid: int,
    ct_by_name: dict[tuple, int],
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> None:
    payload = bundle.get("enterprises") or []
    devices = await _device_registry(session, payload, ct_by_name, report)

    existing = {
        row.enterprise_name: row
        for row in await _scalars(
            session, select(Enterprise).where(Enterprise.branch_id == bid)
        )
    }
    wanted: dict[str, Enterprise] = {}
    for item in payload:
        name = item["name"]
        key = _ref_key(item.get("ref"))
        target = ref_to_id[key] if key else None
        is_dpd = bool(key) and key[0] == "dpd"
        values = {
            "line_id": None if is_dpd else target,
            "dpd_line_id": target if is_dpd else None,
            "active": bool(item.get("active", True)),
            "enabled": bool(item.get("enabled", True)),
        }
        row = existing.get(name)
        if row is None:
            row = Enterprise(branch_id=bid, enterprise_name=name, **values)
            session.add(row)
            report.add("enterprise")
        elif _apply(row, values):
            report.upd("enterprise")
        wanted[name] = row
    await session.flush()

    for name in existing:
        if name not in wanted:
            report.leftover("enterprise", name)

    ent_ids = [e.id for e in wanted.values()] or [-1]
    links = {
        (row.enterprise_id, row.installed_from): row
        for row in await _scalars(
            session,
            select(EnterpriseDevice).where(EnterpriseDevice.enterprise_id.in_(ent_ids)),
        )
    }
    seen: set[tuple] = set()
    for item in payload:
        parent = wanted[item["name"]]
        for dev in item.get("devices") or []:
            installed = _dt(dev.get("installed_from"), f"Підприємство «{item['name']}»")
            key = (parent.id, installed)
            seen.add(key)
            values = {
                "device_id": devices[_device_key(dev, ct_by_name)],
                "removed_at": _dt(dev.get("removed_at"), f"Підприємство «{item['name']}»"),
            }
            row = links.get(key)
            if row is None:
                session.add(
                    EnterpriseDevice(
                        enterprise_id=parent.id, installed_from=installed, **values
                    )
                )
                report.add("enterprise_device")
            elif _apply(row, values):
                report.upd("enterprise_device")
    for key in links:
        if key not in seen:
            report.warnings.append(
                "Історія підприємства містить запис, якого немає у файлі "
                f"(встановлено {key[1]}) — його залишено."
            )


def _device_key(dev: dict, ct_by_name: dict[tuple, int]) -> tuple:
    ct_id, _ = _resolve_corrector(dev, ct_by_name, model_required=False)
    if ct_id is not None:
        return (int(dev["ser_num"]), ct_id, int(dev.get("ch_num") or 0))
    return (
        int(dev["ser_num"]),
        None,
        int(dev.get("ch_num") or 0),
        _as_int(dev.get("mf_dev")),
        _as_int(dev.get("type_dev")),
    )


async def _device_registry(
    session: AsyncSession,
    payload: list[dict],
    ct_by_name: dict[tuple, int],
    report: BranchImportReport,
) -> dict[tuple, int]:
    """(natural key) → dpd_device.id, creating correctors the file introduces.

    `dpd_device` is a pool shared by every branch — a corrector has no
    branch_id — so this upserts rather than inserts: the same instrument moved
    between branches must keep one row and one archive.
    """
    registry: dict[tuple, int] = {}
    for row in await _scalars(session, select(DpdDevice)):
        if row.corector_type_id is not None:
            registry[(row.ser_num, row.corector_type_id, row.ch_num)] = row.id
        else:
            registry[(row.ser_num, None, row.ch_num, row.mf_dev, row.type_dev)] = row.id

    for item in payload:
        for dev in item.get("devices") or []:
            key = _device_key(dev, ct_by_name)
            if key in registry:
                continue
            ct_id, _ = _resolve_corrector(dev, ct_by_name, model_required=False)
            row = DpdDevice(
                ser_num=int(dev["ser_num"]),
                corector_type_id=ct_id,
                ch_num=int(dev.get("ch_num") or 0),
                mf_dev=None if ct_id else _as_int(dev.get("mf_dev")),
                type_dev=None if ct_id else _as_int(dev.get("type_dev")),
            )
            session.add(row)
            await session.flush()
            registry[key] = row.id
            report.add("dpd_device")
    return registry


async def _write_device_mappings(
    session: AsyncSession,
    bundle: dict,
    bid: int,
    ref_to_id: dict[tuple, int],
    report: BranchImportReport,
) -> None:
    payload = bundle.get("device_mappings") or []
    if not payload:
        return
    existing = {
        (row.ser_num, row.mf_dev, row.type_dev, row.ch_num): row
        for row in await _scalars(
            session,
            select(GrmuBranchDeviceMapping).where(GrmuBranchDeviceMapping.branch_id == bid),
        )
    }
    for item in payload:
        key = (
            int(item["ser_num"]),
            _as_int(item.get("mf_dev")),
            _as_int(item.get("type_dev")),
            int(item.get("ch_num") or 0),
        )
        ref = _ref_key(item.get("ref"))
        values = {
            "line_id": ref_to_id.get(ref) if ref else None,
            "grmu_branch_name": item.get("grmu_branch_name"),
            "counterpart": item.get("counterpart"),
            "sector": item.get("sector"),
            "status": item.get("status"),
            "status_changed_at": _dt(item.get("status_changed_at"), "Маппінг приладів"),
            "device_type": item.get("device_type"),
            "manufacturer": item.get("manufacturer"),
            "active": item.get("active"),
        }
        row = existing.get(key)
        if row is None:
            session.add(
                GrmuBranchDeviceMapping(
                    branch_id=bid,
                    ser_num=key[0],
                    mf_dev=key[1],
                    type_dev=key[2],
                    ch_num=key[3],
                    **values,
                )
            )
            report.add("grmu_branch_device_mapping")
        elif _apply(row, values):
            report.upd("grmu_branch_device_mapping")
