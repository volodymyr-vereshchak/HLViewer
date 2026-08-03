"""CRUD tests for /gas-volume-calcs/."""


class TestGasVolumeCalcs:
    async def test_create_without_c_time(self, admin_client, seed_topology):
        """c_time is NOT NULL in the table but carries no meaning: the config
        reader writes the same 7 for every calculator it ingests and nothing
        reads it back. Creating one must not require it — the admin form has no
        such field, and demanding it made every create fail with a 422."""
        resp = await admin_client.post(
            "/gas-volume-calcs/",
            json={"name": "Новий", "address": 33, "lumg_id": seed_topology["lumg"]},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["c_time"] == 7

    async def test_create_keeps_an_explicit_c_time(self, admin_client, seed_topology):
        """The old admin panel sends 600 of its own. A default must not overwrite
        a value the caller did supply."""
        resp = await admin_client.post(
            "/gas-volume-calcs/",
            json={
                "name": "Явний",
                "address": 34,
                "lumg_id": seed_topology["lumg"],
                "c_time": 600,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["c_time"] == 600

    async def test_duplicate_address_in_one_lumg_409(self, admin_client, seed_topology):
        payload = {"name": "Дубль", "address": 12, "lumg_id": seed_topology["lumg"]}
        resp = await admin_client.post("/gas-volume-calcs/", json=payload)
        assert resp.status_code == 409, resp.text

    async def test_missing_address_names_the_field(self, admin_client, seed_topology):
        """address stays required — it is the calculator's identity on the bus."""
        resp = await admin_client.post(
            "/gas-volume-calcs/",
            json={"name": "Без адреси", "lumg_id": seed_topology["lumg"]},
        )
        assert resp.status_code == 422
        assert [d["loc"][-1] for d in resp.json()["detail"]] == ["address"]
