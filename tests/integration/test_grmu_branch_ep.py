"""GRMU branch endpoints: branch CRUD, data path, GIS→LUMG config mappings
(including update-names driven by a synthetic ask.cfg) and DPD credentials
(password must never be returned)."""

from tests.integration.test_config_reader import _cfg_bytes, _gis_block, _write_cfg


async def _create_branch(client, name="Філія Тест") -> dict:
    resp = await client.post("/grmu_branch/", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestBranchCrud:
    async def test_create_get_list(self, admin_client):
        branch = await _create_branch(admin_client)
        assert branch["active"] is True

        single = await admin_client.get(f"/grmu_branch/{branch['id']}")
        assert single.status_code == 200
        assert single.json()["name"] == "Філія Тест"

        listed = (await admin_client.get("/grmu_branch/")).json()
        assert [b["id"] for b in listed] == [branch["id"]]

    async def test_active_only_filter(self, admin_client):
        active = await _create_branch(admin_client, "Активна")
        passive = await _create_branch(admin_client, "Неактивна")
        await admin_client.patch(
            f"/grmu_branch/{passive['id']}", json={"active": False}
        )

        listed = (
            await admin_client.get("/grmu_branch/", params={"active_only": True})
        ).json()
        assert [b["id"] for b in listed] == [active["id"]]

    async def test_update_and_delete(self, admin_client):
        branch = await _create_branch(admin_client)
        patched = await admin_client.patch(
            f"/grmu_branch/{branch['id']}",
            json={"short_name": "ФТ", "region": "Схід"},
        )
        assert patched.status_code == 200
        assert patched.json()["short_name"] == "ФТ"

        assert (
            await admin_client.delete(f"/grmu_branch/{branch['id']}")
        ).status_code == 204
        assert (
            await admin_client.get(f"/grmu_branch/{branch['id']}")
        ).status_code == 404


class TestBranchDataPath:
    async def test_upsert_get_delete(self, admin_client):
        branch = await _create_branch(admin_client)

        put = await admin_client.put(
            f"/grmu_branch/{branch['id']}/data-path",
            json={"path": r"D:\archives\branch", "active": True},
        )
        assert put.status_code == 200

        got = await admin_client.get(f"/grmu_branch/{branch['id']}/data-path")
        assert got.json()["path"] == r"D:\archives\branch"

        # second PUT updates in place
        put2 = await admin_client.put(
            f"/grmu_branch/{branch['id']}/data-path",
            json={"path": r"D:\archives\new", "active": False},
        )
        assert put2.json()["path"] == r"D:\archives\new"
        assert put2.json()["active"] is False

        assert (
            await admin_client.delete(f"/grmu_branch/{branch['id']}/data-path")
        ).status_code == 204
        assert (
            await admin_client.get(f"/grmu_branch/{branch['id']}/data-path")
        ).status_code == 404

    async def test_upsert_for_missing_branch_404(self, admin_client):
        resp = await admin_client.put(
            "/grmu_branch/9999/data-path", json={"path": "x", "active": True}
        )
        assert resp.status_code == 404


class TestConfigMappingsAndPreview:
    async def test_mappings_replace_existing(self, admin_client, seed_topology):
        branch_id = seed_topology["branch"]
        put = await admin_client.put(
            f"/grmu_branch/{branch_id}/config-mappings",
            json=[{"gis_name": "TESTGIS", "lumg_id": seed_topology["lumg"]}],
        )
        assert put.status_code == 200

        # replace with a different set
        put2 = await admin_client.put(
            f"/grmu_branch/{branch_id}/config-mappings",
            json=[
                {"gis_name": "OTHERGIS", "lumg_id": None},
                {"gis_name": "TESTGIS", "lumg_id": seed_topology["lumg"]},
            ],
        )
        assert put2.status_code == 200

        got = (await admin_client.get(f"/grmu_branch/{branch_id}/config-mappings")).json()
        assert {m["gis_name"] for m in got} == {"TESTGIS", "OTHERGIS"}

    async def test_config_preview(self, admin_client, seed_topology, tmp_path):
        branch_id = seed_topology["branch"]
        cfg_path = _write_cfg(tmp_path, _cfg_bytes(_gis_block("TESTGIS")))
        await admin_client.put(
            f"/grmu_branch/{branch_id}/data-path",
            json={"path": cfg_path, "active": True},
        )

        resp = await admin_client.get(f"/grmu_branch/{branch_id}/config-preview")
        assert resp.status_code == 200
        assert resp.json() == [
            {"gis_name": "TESTGIS", "flow_count": 1, "line_count": 1}
        ]

    async def test_preview_without_path_404(self, admin_client, seed_topology):
        resp = await admin_client.get(
            f"/grmu_branch/{seed_topology['branch']}/config-preview"
        )
        assert resp.status_code == 404

    async def test_update_names_full_flow(self, admin_client, seed_topology, tmp_path):
        """End-to-end: data path + mapping + synthetic CFG → line names renamed."""
        branch_id = seed_topology["branch"]
        cfg_path = _write_cfg(tmp_path, _cfg_bytes(_gis_block("TESTGIS")))
        await admin_client.put(
            f"/grmu_branch/{branch_id}/data-path",
            json={"path": cfg_path, "active": True},
        )
        await admin_client.put(
            f"/grmu_branch/{branch_id}/config-mappings",
            json=[{"gis_name": "TESTGIS", "lumg_id": seed_topology["lumg"]}],
        )

        resp = await admin_client.post(f"/grmu_branch/{branch_id}/update-names")
        assert resp.status_code == 200

        # calc a12 → "GRS-1", line l1 → "Line-A" (names from the CFG)
        from sqlmodel import select

        from backend.db.engine import async_session_factory
        from backend.db.models import GasVolumeCalc, Line

        async with async_session_factory() as session:
            calc = (
                await session.execute(
                    select(GasVolumeCalc).where(GasVolumeCalc.id == seed_topology["calc"])
                )
            ).scalar_one()
            line = (
                await session.execute(
                    select(Line).where(Line.id == seed_topology["line1"])
                )
            ).scalar_one()
        assert calc.name == "GRS-1"
        assert line.name == "Line-A"

    async def test_update_names_without_mappings_400(
        self, admin_client, seed_topology, tmp_path
    ):
        branch_id = seed_topology["branch"]
        cfg_path = _write_cfg(tmp_path, _cfg_bytes(_gis_block("TESTGIS")))
        await admin_client.put(
            f"/grmu_branch/{branch_id}/data-path",
            json={"path": cfg_path, "active": True},
        )
        resp = await admin_client.post(f"/grmu_branch/{branch_id}/update-names")
        assert resp.status_code == 400


class TestDpdCredentials:
    async def test_upsert_hides_password(self, admin_client):
        branch = await _create_branch(admin_client)
        put = await admin_client.put(
            f"/grmu_branch/{branch['id']}/dpd-credential",
            json={
                "username": "dpd-user",
                "password": "dpd-secret",
                "api_base_url": "https://api.example/КОД_ФІЛІЇ",
            },
        )
        assert put.status_code == 200
        assert "password" not in put.json()

        got = await admin_client.get(f"/grmu_branch/{branch['id']}/dpd-credential")
        body = got.json()
        assert body["username"] == "dpd-user"
        assert "password" not in body
        assert "dpd-secret" not in got.text

    async def test_create_requires_username_and_password(self, admin_client):
        branch = await _create_branch(admin_client)
        resp = await admin_client.put(
            f"/grmu_branch/{branch['id']}/dpd-credential",
            json={"username": "only-user"},
        )
        assert resp.status_code == 422

    async def test_partial_update_and_delete(self, admin_client):
        branch = await _create_branch(admin_client)
        await admin_client.put(
            f"/grmu_branch/{branch['id']}/dpd-credential",
            json={"username": "dpd-user", "password": "dpd-secret"},
        )
        # partial update (no password required once it exists)
        patch = await admin_client.put(
            f"/grmu_branch/{branch['id']}/dpd-credential",
            json={"timeout_sec": 60},
        )
        assert patch.status_code == 200
        assert patch.json()["timeout_sec"] == 60

        assert (
            await admin_client.delete(f"/grmu_branch/{branch['id']}/dpd-credential")
        ).status_code == 204
        assert (
            await admin_client.get(f"/grmu_branch/{branch['id']}/dpd-credential")
        ).status_code == 404

    async def test_viewer_cannot_read_credentials(self, viewer_client):
        # "dpd-credential" is an admin path marker in the auth middleware
        resp = await viewer_client.get("/grmu_branch/1/dpd-credential")
        assert resp.status_code == 403
