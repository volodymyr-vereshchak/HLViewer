"""Integration-level conftest: every test in this package gets a truncated
(empty) database automatically and carries the `integration` marker."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.api.main import app
from backend.db.engine import async_session_factory
from backend.db.models import GasVolumeCalc, Line, Lumg
from backend.db.models.app_user_model import AppUserBranchAccess
from backend.db.models.grmu_branch_model import GrmuBranch

from tests.conftest import VIEWER_PASSWORD, VIEWER_USERNAME


def pytest_collection_modifyitems(items):
    for item in items:
        if "/tests/integration/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _auto_clean_db(clean_db):
    """Autouse wrapper: forces the TRUNCATE-before-test isolation for the whole
    integration package (unit tests stay DB-free)."""
    yield


@pytest_asyncio.fixture
async def seed_topology(clean_db) -> dict:
    """Minimal device topology: branch → lumg → calc(address=12) → lines 1 and 2.
    Returns the created ids: {'branch', 'lumg', 'calc', 'line1', 'line2'}."""
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.flush()

        lumg = Lumg(name="TESTLUMG", branch_id=branch.id)
        session.add(lumg)
        await session.flush()

        calc = GasVolumeCalc(address=12, name="a12", c_time=7, lumg_id=lumg.id)
        session.add(calc)
        await session.flush()

        line1 = Line(line=1, name="l1", meter=False, gas_volume_calc_id=calc.id)
        line2 = Line(line=2, name="l2", meter=False, gas_volume_calc_id=calc.id)
        session.add(line1)
        session.add(line2)
        await session.commit()
        return {
            "branch": branch.id,
            "lumg": lumg.id,
            "calc": calc.id,
            "line1": line1.id,
            "line2": line2.id,
        }


@pytest_asyncio.fixture
async def seed_two_branches(clean_db) -> dict:
    """Two isolated branches, each with its own lumg → calc → single line.
    Used for viewer branch-scoping tests. Returns
    {'branch1','branch2','lumg1','lumg2','calc1','calc2','line1','line2'}."""
    ids = {}
    async with async_session_factory() as session:
        for n, address in ((1, 10), (2, 20)):
            branch = GrmuBranch(name=f"Філія {n}")
            session.add(branch)
            await session.flush()
            lumg = Lumg(name=f"LUMG{n}", branch_id=branch.id)
            session.add(lumg)
            await session.flush()
            calc = GasVolumeCalc(
                address=address, name=f"a{address}", c_time=7, lumg_id=lumg.id
            )
            session.add(calc)
            await session.flush()
            line = Line(line=1, name=f"l{n}", meter=False, gas_volume_calc_id=calc.id)
            session.add(line)
            await session.flush()
            ids[f"branch{n}"] = branch.id
            ids[f"lumg{n}"] = lumg.id
            ids[f"calc{n}"] = calc.id
            ids[f"line{n}"] = line.id
        await session.commit()
    return ids


@pytest_asyncio.fixture
async def scoped_viewer_client(seed_users, seed_two_branches):
    """Viewer restricted to branch1 only. Access is granted BEFORE login because
    the allowed branches are embedded into the JWT at login time."""
    async with async_session_factory() as session:
        session.add(
            AppUserBranchAccess(
                user_id=seed_users["viewer"], branch_id=seed_two_branches["branch1"]
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/auth/login",
            json={"username": VIEWER_USERNAME, "password": VIEWER_PASSWORD},
        )
        assert resp.status_code == 200
        yield client
