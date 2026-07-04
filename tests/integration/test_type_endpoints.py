"""CRUD tests for the reference-type endpoints:
/sys-types/, /edit-types/ and /gas-volume-calc-types/."""


class TestSysTypes:
    _payload = {"sys_type_id": 7, "gas_volume_calc_type_id": 4, "sys_name": "Втрата живлення"}

    async def test_create_and_paged_list(self, admin_client):
        resp = await admin_client.post("/sys-types/", json=self._payload)
        assert resp.status_code == 201

        resp = await admin_client.get("/sys-types/")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["sys_name"] == "Втрата живлення"

    async def test_duplicate_409(self, admin_client):
        assert (await admin_client.post("/sys-types/", json=self._payload)).status_code == 201
        resp = await admin_client.post("/sys-types/", json=self._payload)
        assert resp.status_code == 409

    async def test_search_and_calc_type_filter(self, admin_client):
        await admin_client.post("/sys-types/", json=self._payload)
        await admin_client.post(
            "/sys-types/",
            json={"sys_type_id": 8, "gas_volume_calc_type_id": 5, "sys_name": "Перевищення тиску"},
        )

        resp = await admin_client.get("/sys-types/", params={"search": "живлення"})
        assert resp.json()["total"] == 1

        resp = await admin_client.get("/sys-types/", params={"calc_type_id": 5})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["sys_type_id"] == 8

    async def test_update_and_delete(self, admin_client):
        created = (await admin_client.post("/sys-types/", json=self._payload)).json()

        # SysTypeUpdate still requires the id fields (only sys_name is optional)
        resp = await admin_client.patch(
            f"/sys-types/{created['id']}",
            json=dict(self._payload, sys_name="Нова назва"),
        )
        assert resp.status_code == 200
        assert resp.json()["sys_name"] == "Нова назва"

        assert (await admin_client.delete(f"/sys-types/{created['id']}")).status_code == 204
        assert (await admin_client.get("/sys-types/")).json()["total"] == 0

    async def test_delete_missing_404(self, admin_client):
        assert (await admin_client.delete("/sys-types/9999")).status_code == 404


class TestEditTypes:
    _payload = {"edit_type_id": 3, "gas_volume_calc_type_id": 4, "edit_name": "Зміна уставки"}

    async def test_crud_cycle(self, admin_client):
        created = await admin_client.post("/edit-types/", json=self._payload)
        assert created.status_code == 201
        entry = created.json()

        assert (await admin_client.post("/edit-types/", json=self._payload)).status_code == 409

        listed = (await admin_client.get("/edit-types/")).json()
        assert listed["total"] == 1

        patched = await admin_client.patch(
            f"/edit-types/{entry['id']}",
            json=dict(self._payload, edit_name="Оновлено"),
        )
        assert patched.json()["edit_name"] == "Оновлено"

        assert (await admin_client.delete(f"/edit-types/{entry['id']}")).status_code == 204
        assert (await admin_client.get("/edit-types/")).json()["total"] == 0


class TestGasVolumeCalcTypes:
    _payload = {"type_id": 4, "type_name": "Флоутек-ТМ"}

    async def test_crud_cycle(self, admin_client):
        created = await admin_client.post("/gas-volume-calc-types/", json=self._payload)
        assert created.status_code == 201

        assert (
            await admin_client.post("/gas-volume-calc-types/", json=self._payload)
        ).status_code == 409

        listed = (await admin_client.get("/gas-volume-calc-types/")).json()
        assert len(listed) == 1
        gvct_id = listed[0]["id"]

        patched = await admin_client.patch(
            f"/gas-volume-calc-types/{gvct_id}", json={"type_name": "Флоутек-ТМ-2"}
        )
        assert patched.status_code == 202
        assert patched.json()["type_name"] == "Флоутек-ТМ-2"

        assert (
            await admin_client.delete(f"/gas-volume-calc-types/{gvct_id}")
        ).status_code == 204
        assert (await admin_client.get("/gas-volume-calc-types/")).json() == []

    async def test_viewer_can_read_not_write(self, viewer_client):
        assert (await viewer_client.get("/gas-volume-calc-types/")).status_code == 200
        resp = await viewer_client.post("/gas-volume-calc-types/", json=self._payload)
        assert resp.status_code == 403  # write blocked by the auth middleware
