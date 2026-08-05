"""Device catalog: CRUD and the JSON the offline server is fed from.

The catalog is not decoration — (mf_dev, type_dev) is the pair DPD is asked
by, so an entry is a device's identity. These tests cover the admin panel's
new editing, the two guards that matter (a model in use cannot be deleted,
the catalog cannot be wiped out from under live devices) and the transfer
between installations in both directions."""

import json

import pytest
import pytest_asyncio

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import CorectorType, Manufacturer
from backend.db.models.enterprise_model import DpdDevice


@pytest.fixture
def catalog_file(tmp_path, monkeypatch):
    """Point `preload` at a throwaway JSON. The repo's own device_catalog.json
    is committed data — a test must never read (or worse, rewrite) it."""
    import backend.db.preload_db.preload_device_catalog as preloader

    target = tmp_path / "device_catalog.json"
    monkeypatch.setattr(preloader, "CATALOG_PATH", target)

    def write(manufacturers: list[dict]) -> None:
        target.write_text(
            json.dumps({"manufacturers": manufacturers}, ensure_ascii=False),
            encoding="utf-8",
        )

    return write


@pytest_asyncio.fixture
async def catalog(clean_db) -> dict:
    async with async_session_factory() as session:
        mfr = Manufacturer(short_name="РадмирТех", full_name="РадмирТех ТОВ СП", mf_dev=1)
        session.add(mfr)
        await session.flush()
        ct = CorectorType(manufacturer_id=mfr.id, model_name="ВЕГА-1.01", type_dev=3)
        session.add(ct)
        await session.commit()
        await session.refresh(mfr)
        await session.refresh(ct)
        return {"manufacturer": mfr.id, "corector_type": ct.id}


class TestManufacturerCrud:
    async def test_create_update_delete(self, admin_client, clean_db):
        created = await admin_client.post(
            "/device-catalog/manufacturers/",
            json={"short_name": "Тандем", "full_name": "Тандем ПП НВФ", "mf_dev": 4},
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]

        patched = await admin_client.patch(
            f"/device-catalog/manufacturers/{item_id}", json={"short_name": "Тандем-2"}
        )
        assert patched.status_code == 200
        assert patched.json()["short_name"] == "Тандем-2"

        assert (
            await admin_client.delete(f"/device-catalog/manufacturers/{item_id}")
        ).status_code == 204

    async def test_duplicate_mf_dev_is_a_named_conflict(self, admin_client, catalog):
        """mf_dev is how DPD addresses the manufacturer, so it cannot repeat."""
        resp = await admin_client.post(
            "/device-catalog/manufacturers/",
            json={"short_name": "Інший", "full_name": "Інший завод", "mf_dev": 1},
        )
        assert resp.status_code == 409
        assert "mf_dev" in resp.json()["detail"]

    async def test_viewer_cannot_edit(self, viewer_client, clean_db):
        resp = await viewer_client.post(
            "/device-catalog/manufacturers/",
            json={"short_name": "Х", "full_name": "Х", "mf_dev": 77},
        )
        assert resp.status_code == 403


class TestCorectorTypeCrud:
    async def test_create_update_delete(self, admin_client, catalog):
        created = await admin_client.post(
            "/device-catalog/corector-types/",
            json={
                "manufacturer_id": catalog["manufacturer"],
                "model_name": "ВЕГА-2.01",
                "type_dev": 8,
            },
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]

        patched = await admin_client.patch(
            f"/device-catalog/corector-types/{item_id}", json={"model_name": "ВЕГА-2.02"}
        )
        assert patched.status_code == 200
        assert patched.json()["model_name"] == "ВЕГА-2.02"

        assert (
            await admin_client.delete(f"/device-catalog/corector-types/{item_id}")
        ).status_code == 204

    async def test_duplicate_model_for_one_manufacturer_is_a_conflict(
        self, admin_client, catalog
    ):
        resp = await admin_client.post(
            "/device-catalog/corector-types/",
            json={
                "manufacturer_id": catalog["manufacturer"],
                "model_name": "ВЕГА-1.01",
                "type_dev": 99,
            },
        )
        assert resp.status_code == 409

    async def test_filter_by_manufacturer(self, admin_client, catalog):
        listed = await admin_client.get(
            "/device-catalog/corector-types/",
            params={"manufacturer_id": catalog["manufacturer"]},
        )
        assert [c["model_name"] for c in listed.json()] == ["ВЕГА-1.01"]


class TestDeleteProtection:
    async def test_model_in_use_cannot_be_deleted(self, admin_client, catalog):
        """A corrector references its type, and the type is what the device is
        polled by — dropping it would leave the device unaddressable and its
        archive orphaned. The panel must say so, not return a 500."""
        async with async_session_factory() as session:
            session.add(DpdDevice(
                ser_num=555, corector_type_id=catalog["corector_type"], ch_num=0
            ))
            await session.commit()

        resp = await admin_client.delete(
            f"/device-catalog/corector-types/{catalog['corector_type']}"
        )
        assert resp.status_code == 409, resp.text
        assert "прилади" in resp.json()["detail"]

    async def test_manufacturer_of_a_used_model_cannot_be_deleted(
        self, admin_client, catalog
    ):
        async with async_session_factory() as session:
            session.add(DpdDevice(
                ser_num=555, corector_type_id=catalog["corector_type"], ch_num=0
            ))
            await session.commit()

        resp = await admin_client.delete(
            f"/device-catalog/manufacturers/{catalog['manufacturer']}"
        )
        assert resp.status_code == 409, resp.text


class TestCatalogTransfer:
    async def test_export_writes_the_db_into_the_preload_json(
        self, admin_client, catalog, tmp_path, monkeypatch
    ):
        """The export is the only way an edit made here reaches another
        installation: the JSON is committed with the code and seeded on the
        offline server. Writing to a temp path so the repo file is untouched."""
        import backend.db.preload_db.export_device_catalog as exporter

        target = tmp_path / "device_catalog.json"
        monkeypatch.setattr(exporter, "CATALOG_PATH", target)

        resp = await admin_client.post("/device-catalog/export-preload")
        assert resp.status_code == 200, resp.text
        assert resp.json()["exported"] == {"manufacturers": 1, "corector_types": 1}

        written = json.loads(target.read_text(encoding="utf-8"))
        assert [m["short_name"] for m in written["manufacturers"]] == ["РадмирТех"]
        assert written["manufacturers"][0]["models"] == [
            {"model_name": "ВЕГА-1.01", "type_dev": 3}
        ]

    async def test_export_is_admin_only(self, viewer_client, catalog):
        resp = await viewer_client.post("/device-catalog/export-preload")
        assert resp.status_code == 403

    async def test_preload_adds_missing_entries_without_touching_existing(
        self, admin_client, catalog
    ):
        """Without `force` the catalog is topped up, not replaced — the shape
        the offline side runs after receiving a new JSON."""
        before = (await admin_client.get("/device-catalog/manufacturers/")).json()
        resp = await admin_client.post("/device-catalog/preload")
        assert resp.status_code == 200, resp.text

        after = (await admin_client.get("/device-catalog/manufacturers/")).json()
        # The seeded manufacturer survived; the file's entries were added.
        assert catalog["manufacturer"] in [m["id"] for m in after]
        assert len(after) >= len(before)


class TestCatalogRename:
    """Carrying a rename from one installation to another.

    A manufacturer is identified by `mf_dev`, so «РадмирТех» → «Радміртех»
    made elsewhere arrives here as a rename of the same row. A model is not:
    its `type_dev` is shared by several models on purpose, and an admin may
    have added more under the same code on this server, so nothing about a
    renamed model can be told apart from a new one. It is added, and the
    disagreement is reported rather than guessed at."""

    async def test_manufacturer_rename_travels_by_code(
        self, admin_client, catalog, catalog_file
    ):
        catalog_file([{
            "short_name": "Радміртех",
            "full_name": "Радміртех ТОВ СП",
            "mf_dev": 1,
            "models": [{"model_name": "ВЕГА-1.01", "type_dev": 3}],
        }])

        resp = await admin_client.post("/device-catalog/preload")
        assert resp.status_code == 200, resp.text
        assert resp.json()["renamed_manufacturers"] == 1
        assert resp.json()["added_manufacturers"] == 0

        listed = (await admin_client.get("/device-catalog/manufacturers/")).json()
        # Renamed in place: one row, same id — every model still hangs off it.
        assert len(listed) == 1
        assert listed[0]["id"] == catalog["manufacturer"]
        assert listed[0]["short_name"] == "Радміртех"
        assert listed[0]["full_name"] == "Радміртех ТОВ СП"

    async def test_renamed_model_arrives_as_a_new_one(
        self, admin_client, catalog, catalog_file
    ):
        catalog_file([{
            "short_name": "РадмирТех",
            "full_name": "РадмирТех ТОВ СП",
            "mf_dev": 1,
            "models": [{"model_name": "ВЕГА-1.02", "type_dev": 3}],
        }])

        resp = await admin_client.post("/device-catalog/preload")
        assert resp.json()["added_models"] == 1

        listed = (await admin_client.get("/device-catalog/corector-types/")).json()
        # The old model keeps its id: devices reference it, so it is never
        # touched. Merging the two is the admin's call, made by hand.
        assert {c["model_name"] for c in listed} == {"ВЕГА-1.01", "ВЕГА-1.02"}
        assert catalog["corector_type"] in [c["id"] for c in listed]

    async def test_type_dev_disagreement_is_reported_not_applied(
        self, admin_client, catalog, catalog_file
    ):
        catalog_file([{
            "short_name": "РадмирТех",
            "full_name": "РадмирТех ТОВ СП",
            "mf_dev": 1,
            "models": [{"model_name": "ВЕГА-1.01", "type_dev": 9}],
        }])

        resp = await admin_client.post("/device-catalog/preload")
        body = resp.json()
        assert body["warnings"], body
        assert "ВЕГА-1.01" in body["warnings"][0]

        listed = (await admin_client.get("/device-catalog/corector-types/")).json()
        assert listed[0]["type_dev"] == 3  # the DB value stands

    async def test_unchanged_file_reports_no_changes(
        self, admin_client, catalog, catalog_file
    ):
        catalog_file([{
            "short_name": "РадмирТех",
            "full_name": "РадмирТех ТОВ СП",
            "mf_dev": 1,
            "models": [{"model_name": "ВЕГА-1.01", "type_dev": 3}],
        }])

        body = (await admin_client.post("/device-catalog/preload")).json()
        assert body["added_manufacturers"] == 0
        assert body["renamed_manufacturers"] == 0
        assert body["added_models"] == 0
        assert body["warnings"] == []

    async def test_force_refuses_while_devices_reference_the_catalog(
        self, admin_client, catalog, catalog_file
    ):
        """«Перезаписати» wipes the catalog first. Models are referenced with
        RESTRICT, so the DB stops it — the panel must explain that instead of
        returning a 500, since the plain merge is what was wanted anyway."""
        async with async_session_factory() as session:
            session.add(DpdDevice(
                ser_num=555, corector_type_id=catalog["corector_type"], ch_num=0
            ))
            await session.commit()

        catalog_file([])
        resp = await admin_client.post("/device-catalog/preload?force=true")
        assert resp.status_code == 409, resp.text
        assert "mf_dev" in resp.json()["detail"]

        # Nothing was lost on the way out.
        listed = (await admin_client.get("/device-catalog/corector-types/")).json()
        assert [c["id"] for c in listed] == [catalog["corector_type"]]
