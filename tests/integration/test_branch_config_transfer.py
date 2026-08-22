"""Branch configuration transfer: export a branch to a JSON bundle, merge that
bundle into another installation.

The suite plays both sides on one database: a fully configured «source» branch
is exported, the bundle is re-pointed at a new name/uid, and the import creates
the second branch. Re-exporting THAT branch has to give the same document back —
which is the only assertion that covers every field at once, including the ones
nobody would think to check individually.
"""

import json
import uuid
from datetime import datetime

import pytest_asyncio
from sqlalchemy import func, select

from backend.db.engine import async_session_factory
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

SOURCE_NAME = "Джерельна філія"
TARGET_NAME = "Центральна копія"
DPD_PASSWORD = "dpd-secret-42"

COUNTED = (
    GrmuBranch, Lumg, GasVolumeCalc, Line, DpdLine, DpdLineDevice,
    VirtualLine, VirtualLineMember, GasRoute, GasRouteMember,
    Enterprise, EnterpriseDevice, DpdDevice,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _row_counts() -> dict:
    async with async_session_factory() as session:
        return {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in COUNTED
        }


async def _export(client, branch_id: int, include_secrets: bool = True) -> dict:
    resp = await client.get(
        f"/grmu_branch/{branch_id}/config-export",
        params={"include_secrets": include_secrets},
    )
    assert resp.status_code == 200, resp.text
    return json.loads(resp.content.decode("utf-8"))


async def _import(
    client,
    bundle: dict,
    dry_run: bool = True,
    *,
    target_branch_id: int | None = None,
    create_new: bool = False,
    lumg_map: dict | None = None,
) -> dict:
    payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    params: dict = {"dry_run": dry_run}
    if target_branch_id is not None:
        params["target_branch_id"] = target_branch_id
    if create_new:
        params["create_new"] = True
    resp = await client.post(
        "/grmu_branch/config-import",
        params=params,
        files={"file": ("bundle.json", payload, "application/json")},
        data={"lumg_map": json.dumps(lumg_map)} if lumg_map is not None else None,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _lumg_names(branch_id: int) -> list[str]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Lumg).where(Lumg.branch_id == branch_id).order_by(Lumg.name)
            )
        ).scalars().all()
        return [r.name for r in rows]


async def _lumg_id(branch_id: int, name: str) -> int:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(Lumg).where(Lumg.branch_id == branch_id, Lumg.name == name)
            )
        ).scalars().first()
        assert row is not None, f"ЛУМГ {name!r} not found"
        return row.id


def _retarget(bundle: dict, name: str = TARGET_NAME) -> dict:
    """Same configuration, new branch identity — the cross-server situation."""
    copy = json.loads(json.dumps(bundle))
    copy["branch"]["uid"] = str(uuid.uuid4())
    copy["branch"]["name"] = name
    return copy


async def _branch_id(name: str) -> int:
    async with async_session_factory() as session:
        row = (
            await session.execute(select(GrmuBranch).where(GrmuBranch.name == name))
        ).scalars().first()
        assert row is not None, f"branch {name!r} not found"
        return row.id


@pytest_asyncio.fixture
async def rich_branch(clean_db) -> dict:
    """A branch that exercises every section of the bundle."""
    async with async_session_factory() as session:
        mfr = Manufacturer(short_name="РадмирТех", full_name="РадмирТех ТОВ", mf_dev=16)
        session.add(mfr)
        await session.flush()
        ct = CorectorType(manufacturer_id=mfr.id, model_name="ВЕГА-1.01", type_dev=1)
        ct2 = CorectorType(manufacturer_id=mfr.id, model_name="ВЕГА-2", type_dev=2)
        session.add(ct)
        session.add(ct2)
        gvct = GasVolumeCalcType(type_id=33, type_name="ФЛОУТЕК")
        session.add(gvct)
        await session.flush()

        branch = GrmuBranch(name=SOURCE_NAME, short_name="ДжФ", region="Схід")
        session.add(branch)
        await session.flush()
        session.add(BranchDataPath(branch_id=branch.id, path=r"\\ask\share", active=True))
        session.add(
            GrmuBranchDpdCredential(
                branch_id=branch.id,
                username="dpd-user",
                password=DPD_PASSWORD,
                api_base_url="https://dpd.example/api",
                auth_url="https://dpd.example/auth",
                timeout_sec=45,
            )
        )

        lumg = Lumg(name="ЛУМГ-1", branch_id=branch.id)
        session.add(lumg)
        await session.flush()
        session.add(LumgDataPath(lumg_id=lumg.id, path="/data/lumg1", active=True))
        session.add(LumgEisCode(lumg_id=lumg.id, eis_code="EIS-1"))
        session.add(LumgEisCode(lumg_id=lumg.id, eis_code="EIS-2"))

        calc = GasVolumeCalc(
            lumg_id=lumg.id, type_id=gvct.id, address=12, name="Обчислювач 12", c_time=7
        )
        session.add(calc)
        await session.flush()
        line1 = Line(
            gas_volume_calc_id=calc.id, line=1, name="Лінія 1", meter=True,
            include_in_report=True, include_in_trends=True, is_high_pressure=True,
            pressure_unit="МПа", dp_unit="кПа",
        )
        line2 = Line(gas_volume_calc_id=calc.id, line=2, name="Лінія 2", meter=False)
        session.add(line1)
        session.add(line2)

        dpd_line = DpdLine(
            branch_id=branch.id, lumg_id=lumg.id, name="ДПД-Схід",
            description="через API", active=True, include_in_report=True,
        )
        session.add(dpd_line)
        await session.flush()
        session.add(
            DpdLineDevice(
                dpd_line_id=dpd_line.id, ser_num=1001, corector_type_id=ct.id,
                ch_num=0, installed_from=datetime(2024, 1, 1, 0, 0),
            )
        )
        session.add(
            DpdLineDevice(
                dpd_line_id=dpd_line.id, ser_num=1002, corector_type_id=ct2.id,
                ch_num=1, installed_from=datetime(2025, 6, 1, 10, 0),
            )
        )

        ring = VirtualLine(
            branch_id=branch.id, lumg_id=lumg.id, name="Кільце-1",
            description="лінія + ДПД", include_in_trends=True,
        )
        session.add(ring)
        await session.flush()
        session.add(
            VirtualLineMember(virtual_line_id=ring.id, line_id=line1.id, sort_order=0)
        )
        session.add(
            VirtualLineMember(
                virtual_line_id=ring.id, dpd_line_id=dpd_line.id, sort_order=1
            )
        )

        route = GasRoute(branch_id=branch.id, number="007", name="Маршрут 7")
        session.add(route)
        await session.flush()
        session.add(
            GasRouteMember(
                route_id=route.id, line_id=line1.id, is_reference=True, sort_order=0
            )
        )
        session.add(GasRouteMember(route_id=route.id, line_id=line2.id, sort_order=1))

        dev_a = DpdDevice(ser_num=2001, corector_type_id=ct.id, ch_num=0)
        dev_b = DpdDevice(ser_num=2002, corector_type_id=ct2.id, ch_num=0)
        session.add(dev_a)
        session.add(dev_b)
        await session.flush()
        ent = Enterprise(
            branch_id=branch.id, enterprise_name="ТОВ Завод", line_id=line1.id
        )
        session.add(ent)
        await session.flush()
        session.add(
            EnterpriseDevice(
                enterprise_id=ent.id, device_id=dev_a.id,
                installed_from=datetime(2024, 1, 1), removed_at=datetime(2025, 3, 5, 8),
            )
        )
        session.add(
            EnterpriseDevice(
                enterprise_id=ent.id, device_id=dev_b.id,
                installed_from=datetime(2025, 3, 10, 9),
            )
        )
        session.add(
            GrmuBranchDeviceMapping(
                branch_id=branch.id, line_id=line2.id, ser_num=3001,
                mf_dev=16, type_dev=1, ch_num=0, counterpart="Контрагент",
            )
        )
        await session.commit()
        return {"branch": branch.id, "line1": line1.id, "calc": calc.id}


# ─── Round trip ───────────────────────────────────────────────────────────────


class TestRoundTrip:
    async def test_export_reimport_export_is_identical(self, admin_client, rich_branch):
        source = await _export(admin_client, rich_branch["branch"])
        bundle = _retarget(source)

        report = await _import(admin_client, bundle, dry_run=False)
        assert report["errors"] == []
        assert report["applied"] is True
        assert report["matched_by"] == "new"

        copy_id = await _branch_id(TARGET_NAME)
        copied = await _export(admin_client, copy_id)

        # Only the identity and the timestamp may differ — plus the data paths,
        # which an import deliberately parks as inactive, and the ЄІС-codes,
        # which are unique across the whole database and are therefore still
        # held by the source branch (TestEisCollision covers that on its own).
        for doc in (source, copied):
            doc.pop("exported_at")
            doc["branch"].pop("uid")
            doc["branch"].pop("name")
        source["branch"]["data_path"]["active"] = False
        source["lumgs"][0]["data_path"]["active"] = False
        source["lumgs"][0]["eis_codes"] = []
        assert copied == source

    async def test_line_flags_and_units_survive(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)

        copy_id = await _branch_id(TARGET_NAME)
        line = (await _export(admin_client, copy_id))["lumgs"][0]["calcs"][0]["lines"][0]
        assert line["meter"] is True
        assert line["include_in_report"] is True
        assert line["is_high_pressure"] is True
        assert (line["pressure_unit"], line["dp_unit"]) == ("МПа", "кПа")

    async def test_enterprise_windows_survive(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)

        copy_id = await _branch_id(TARGET_NAME)
        ent = (await _export(admin_client, copy_id))["enterprises"][0]
        assert ent["ref"]["kind"] == "physical"
        assert [d["removed_at"] for d in ent["devices"]] == [
            "2025-03-05T08:00:00",
            None,
        ]
        assert [d["model_name"] for d in ent["devices"]] == ["ВЕГА-1.01", "ВЕГА-2"]

    async def test_ring_keeps_its_dpd_member(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)

        copy_id = await _branch_id(TARGET_NAME)
        ring = (await _export(admin_client, copy_id))["virtual_lines"][0]
        assert [m["ref"]["kind"] for m in ring["members"]] == ["physical", "dpd"]

    async def test_route_reference_line_survives(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)

        copy_id = await _branch_id(TARGET_NAME)
        route = (await _export(admin_client, copy_id))["gas_routes"][0]
        assert [m["is_reference"] for m in route["members"]] == [True, False]

    async def test_correctors_are_reused_not_duplicated(self, admin_client, rich_branch):
        """A corrector is a shared pool row: the same instrument on two branches
        must stay one `dpd_device`, or its DPD archive would split in half."""
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        before = (await _row_counts())["dpd_device"]
        await _import(admin_client, bundle, dry_run=False)
        assert (await _row_counts())["dpd_device"] == before


# ─── Merge semantics ──────────────────────────────────────────────────────────


class TestMergeSemantics:
    async def test_second_import_changes_nothing(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)

        counts = await _row_counts()
        report = await _import(admin_client, bundle, dry_run=False)
        assert report["created"] == {}
        assert report["updated"] == {}
        assert await _row_counts() == counts

    async def test_dry_run_writes_nothing(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        before = await _row_counts()

        report = await _import(admin_client, bundle, dry_run=True)
        assert report["applied"] is False
        assert report["created"]["gas_volume_line"] == 2
        assert await _row_counts() == before

    async def test_local_only_rows_are_listed_not_deleted(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)
        copy_id = await _branch_id(TARGET_NAME)

        async with async_session_factory() as session:
            calc = (
                await session.execute(
                    select(GasVolumeCalc)
                    .join(Lumg, GasVolumeCalc.lumg_id == Lumg.id)
                    .where(Lumg.branch_id == copy_id)
                )
            ).scalars().first()
            session.add(
                Line(gas_volume_calc_id=calc.id, line=9, name="Лише тут", meter=False)
            )
            await session.commit()

        report = await _import(admin_client, bundle, dry_run=False)
        assert report["local_only"]["gas_volume_line"] == ["Лише тут"]

        async with async_session_factory() as session:
            still_there = (
                await session.execute(select(Line).where(Line.name == "Лише тут"))
            ).scalars().first()
        assert still_there is not None

    async def test_matched_by_name_adopts_the_uid(self, admin_client, rich_branch):
        """First transfer onto a server where the branch was created by hand."""
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        async with async_session_factory() as session:
            session.add(GrmuBranch(name=TARGET_NAME))
            await session.commit()

        report = await _import(admin_client, bundle, dry_run=False)
        assert report["matched_by"] == "name"
        assert any("за назвою" in w for w in report["warnings"])

        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(GrmuBranch).where(GrmuBranch.name == TARGET_NAME)
                )
            ).scalars().first()
        assert str(row.export_uid) == bundle["branch"]["uid"]

    async def test_new_data_paths_arrive_inactive(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        assert bundle["branch"]["data_path"]["active"] is True

        report = await _import(admin_client, bundle, dry_run=False)
        assert any("неактивними" in w for w in report["warnings"])

        copy_id = await _branch_id(TARGET_NAME)
        copied = await _export(admin_client, copy_id)
        assert copied["branch"]["data_path"]["active"] is False
        assert copied["lumgs"][0]["data_path"]["active"] is False

    async def test_reimport_leaves_a_live_path_alone(self, admin_client, rich_branch):
        """Importing a bundle back onto the branch that produced it must not
        switch off that branch's own polling."""
        bundle = await _export(admin_client, rich_branch["branch"])
        report = await _import(admin_client, bundle, dry_run=False)
        assert report["matched_by"] == "uid"
        assert not any("неактивними" in w for w in report["warnings"])

        again = await _export(admin_client, rich_branch["branch"])
        assert again["branch"]["data_path"]["active"] is True
        assert again["lumgs"][0]["data_path"]["active"] is True


# ─── Secrets ──────────────────────────────────────────────────────────────────


class TestSecrets:
    async def test_password_travels_by_default(self, admin_client, rich_branch):
        bundle = await _export(admin_client, rich_branch["branch"])
        assert bundle["includes_secrets"] is True
        assert bundle["branch"]["dpd_credential"]["password"] == DPD_PASSWORD

    async def test_password_can_be_withheld(self, admin_client, rich_branch):
        bundle = await _export(admin_client, rich_branch["branch"], include_secrets=False)
        assert bundle["includes_secrets"] is False
        assert "password" not in bundle["branch"]["dpd_credential"]
        assert bundle["branch"]["dpd_credential"]["username"] == "dpd-user"

    async def test_import_without_password_keeps_the_existing_one(
        self, admin_client, rich_branch
    ):
        full = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, full, dry_run=False)
        copy_id = await _branch_id(TARGET_NAME)

        stripped = await _export(admin_client, copy_id, include_secrets=False)
        stripped["branch"]["dpd_credential"]["timeout_sec"] = 90
        report = await _import(admin_client, stripped, dry_run=False)
        assert any("без змін" in w for w in report["warnings"])

        async with async_session_factory() as session:
            cred = (
                await session.execute(
                    select(GrmuBranchDpdCredential).where(
                        GrmuBranchDpdCredential.branch_id == copy_id
                    )
                )
            ).scalars().first()
        assert cred.password == DPD_PASSWORD
        assert cred.timeout_sec == 90


# ─── Refusals ─────────────────────────────────────────────────────────────────


class TestRefusals:
    async def test_not_a_bundle(self, admin_client, rich_branch):
        report = await _import(admin_client, {"hello": "world"})
        assert report["applied"] is False
        assert "не файл конфігурації" in report["errors"][0]

    async def test_wrong_version(self, admin_client, rich_branch):
        bundle = await _export(admin_client, rich_branch["branch"])
        bundle["version"] = 99
        report = await _import(admin_client, bundle)
        assert any("Версія формату" in e for e in report["errors"])

    async def test_name_belongs_to_another_branch(self, admin_client, rich_branch):
        """The uid points at branch A while the name is held by branch B —
        applying would rename A onto a taken name."""
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)

        bundle["branch"]["name"] = SOURCE_NAME
        report = await _import(admin_client, bundle, dry_run=False)
        assert report["applied"] is False
        assert any("вже носить інша філія" in e for e in report["errors"])

    async def test_duplicate_enterprise_name_in_file(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        bundle["enterprises"].append(json.loads(json.dumps(bundle["enterprises"][0])))
        report = await _import(admin_client, bundle)
        assert any("двічі" in e for e in report["errors"])

    async def test_member_line_missing_from_file(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        bundle["lumgs"][0]["calcs"][0]["lines"] = [
            ln for ln in bundle["lumgs"][0]["calcs"][0]["lines"] if ln["line"] != 1
        ]
        report = await _import(admin_client, bundle)
        assert any("у файлі немає лінії" in e for e in report["errors"])

    async def test_unknown_corrector_model(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        bundle["dpd_lines"][0]["devices"][0]["model_name"] = "НЕВІДОМА-9"
        report = await _import(admin_client, bundle)
        assert report["applied"] is False
        assert any("НЕВІДОМА-9" in e for e in report["errors"])
        assert any("довіднику коректорів" in e for e in report["errors"])

    async def test_unknown_calc_type(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        bundle["lumgs"][0]["calcs"][0]["type_id"] = 999
        report = await _import(admin_client, bundle)
        assert any("FLOWTYPE.json" in e for e in report["errors"])

    async def test_errors_block_every_write(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        bundle["lumgs"][0]["calcs"][0]["type_id"] = 999
        before = await _row_counts()
        await _import(admin_client, bundle, dry_run=False)
        assert await _row_counts() == before

    async def test_broken_json_is_a_400(self, admin_client):
        resp = await admin_client.post(
            "/grmu_branch/config-import",
            files={"file": ("bundle.json", b"{not json", "application/json")},
        )
        assert resp.status_code == 400
        assert "JSON" in resp.json()["detail"]


class TestEisCollision:
    async def test_code_taken_by_another_lumg_is_skipped(self, admin_client, rich_branch):
        """`lumg_eis_code.eis_code` is unique across the whole database, so on a
        server holding several branches two of them can claim the same code."""
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        report = await _import(admin_client, bundle, dry_run=False)
        assert report["errors"] == []
        assert any("ЄІС-код EIS-1" in w for w in report["warnings"])

        async with async_session_factory() as session:
            owners = (
                await session.execute(
                    select(func.count()).select_from(LumgEisCode).where(
                        LumgEisCode.eis_code == "EIS-1"
                    )
                )
            ).scalar_one()
        assert owners == 1


class TestChoosingTheTarget:
    """The file cannot know which rows here it IS — the administrator says so.

    Everything below is the case that used to go wrong silently: a name changed
    on one of the two sides, so nothing matched and a second copy appeared.
    """

    async def test_renamed_branch_without_a_choice_makes_a_second_one(
        self, admin_client, rich_branch
    ):
        """Why the choice exists at all: a first transfer of a branch that has
        since been renamed matches nothing and duplicates it."""
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        report = await _import(admin_client, bundle, dry_run=True)
        assert report["matched_by"] == "new"
        assert report["created"]["grmu_branch"] == 1

    async def test_chosen_branch_is_updated_and_renamed(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        async with async_session_factory() as session:
            session.add(GrmuBranch(name="Стара назва"))
            await session.commit()
        stale_id = await _branch_id("Стара назва")

        report = await _import(
            admin_client, bundle, dry_run=False, target_branch_id=stale_id
        )
        assert report["errors"] == []
        assert report["matched_by"] == "chosen"
        assert report["branch_id"] == stale_id
        assert any("перейменовано" in w for w in report["warnings"])
        assert "grmu_branch" not in report["created"]

        # One branch, not two, and it carries the file's identity now.
        assert await _branch_id(TARGET_NAME) == stale_id

    async def test_create_new_is_explicit(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        report = await _import(admin_client, bundle, dry_run=False, create_new=True)
        assert report["errors"] == []
        assert report["created"]["grmu_branch"] == 1

    async def test_create_new_refuses_a_taken_name(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)
        report = await _import(admin_client, bundle, dry_run=True, create_new=True)
        assert any("вже переносився" in e for e in report["errors"])

    async def test_cannot_point_a_bundle_at_a_foreign_branch(
        self, admin_client, rich_branch
    ):
        """The bundle already belongs to one branch; aiming it at another would
        give two branches the same transfer id."""
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)
        async with async_session_factory() as session:
            session.add(GrmuBranch(name="Третя філія"))
            await session.commit()
        other = await _branch_id("Третя філія")

        report = await _import(admin_client, bundle, target_branch_id=other)
        assert any("вже переносився" in e for e in report["errors"])

    async def test_both_choices_at_once_is_refused(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        resp = await admin_client.post(
            "/grmu_branch/config-import",
            params={"dry_run": True, "target_branch_id": rich_branch["branch"],
                    "create_new": True},
            files={
                "file": (
                    "b.json", json.dumps(bundle, ensure_ascii=False).encode(), "application/json",
                )
            },
        )
        assert resp.status_code == 200
        assert any("щось одне" in e for e in resp.json()["errors"])


class TestLumgMapping:
    async def test_renamed_lumg_without_mapping_duplicates_the_tree(
        self, admin_client, rich_branch
    ):
        """The failure the warning is there to prevent."""
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)
        copy_id = await _branch_id(TARGET_NAME)

        bundle["lumgs"][0]["name"] = "ЛУМГ-Перейменований"
        for section in ("dpd_lines", "virtual_lines"):
            for item in bundle[section]:
                item["lumg"] = "ЛУМГ-Перейменований"
        for section in ("virtual_lines", "gas_routes", "enterprises"):
            for item in bundle[section]:
                members = item.get("members") or ([item] if "ref" in item else [])
                for m in members:
                    if m.get("ref") and m["ref"].get("kind") == "physical":
                        m["ref"]["lumg"] = "ЛУМГ-Перейменований"

        report = await _import(admin_client, bundle, dry_run=True)
        assert report["new_lumgs"] == ["ЛУМГ-Перейменований"]
        assert [u["name"] for u in report["unmatched_lumgs"]] == ["ЛУМГ-1"]
        assert any("подвоїться" in w for w in report["warnings"])
        assert report["created"]["gas_volume_line"] == 2

        # And with the mapping the same file renames instead of duplicating.
        mapped = await _import(
            admin_client,
            bundle,
            dry_run=False,
            lumg_map={"ЛУМГ-Перейменований": await _lumg_id(copy_id, "ЛУМГ-1")},
        )
        assert mapped["errors"] == []
        assert "lumg" not in mapped["created"]
        assert "gas_volume_line" not in mapped["created"]
        assert await _lumg_names(copy_id) == ["ЛУМГ-Перейменований"]

    async def test_mapping_to_a_foreign_lumg_is_refused(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)
        source_lumg = await _lumg_id(rich_branch["branch"], "ЛУМГ-1")

        bundle["lumgs"][0]["name"] = "Інша назва"
        report = await _import(
            admin_client, bundle, lumg_map={"Інша назва": source_lumg}
        )
        assert any("не належить цій філії" in e for e in report["errors"])

    async def test_two_file_lumgs_cannot_share_one_row(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        await _import(admin_client, bundle, dry_run=False)
        copy_id = await _branch_id(TARGET_NAME)
        target = await _lumg_id(copy_id, "ЛУМГ-1")

        bundle["lumgs"].append(
            {"name": "ЛУМГ-2", "data_path": None, "eis_codes": [], "calcs": []}
        )
        report = await _import(
            admin_client,
            bundle,
            lumg_map={"ЛУМГ-1": target, "ЛУМГ-2": target},
        )
        assert any("одразу з" in e for e in report["errors"])

    async def test_no_warning_when_every_lumg_matches(self, admin_client, rich_branch):
        bundle = _retarget(await _export(admin_client, rich_branch["branch"]))
        report = await _import(admin_client, bundle, dry_run=True)
        assert report["new_lumgs"] == ["ЛУМГ-1"]
        # Nothing to confuse it with on a branch that does not exist yet.
        assert report["unmatched_lumgs"] == []
        assert not any("подвоїться" in w for w in report["warnings"])


class TestPermissions:
    async def test_viewer_cannot_export(self, viewer_client, rich_branch):
        resp = await viewer_client.get(
            f"/grmu_branch/{rich_branch['branch']}/config-export"
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_import(self, viewer_client, rich_branch):
        resp = await viewer_client.post(
            "/grmu_branch/config-import",
            files={"file": ("bundle.json", b"{}", "application/json")},
        )
        assert resp.status_code == 403

    async def test_export_of_unknown_branch_is_404(self, admin_client, rich_branch):
        resp = await admin_client.get("/grmu_branch/999999/config-export")
        assert resp.status_code == 404
