"""The constraints the GSM polling schema leans on.

Step 1 of docs/plans/gsm-polling.md adds tables nothing reads yet, so almost
nothing here is worth testing — except the two rules the rest of the feature
will assume without checking:

  * a poll card points at exactly one site — an enterprise metering point or
    a DPD line;
  * a site has at most one card.

Both are expressed in the schema rather than in code, which only works if the
schema really carries them: tests build it from SQLModel.metadata and
production from Alembic, and nothing else compares the two.
"""

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from backend.db.engine import async_session_factory
from backend.db.models.dpd_line_model import DpdLine
from backend.db.models.enterprise_model import Enterprise
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.db.models.polling_model import PollAgent, PollAgentDevice, PollDevice


@pytest_asyncio.fixture
async def targets(clean_db) -> dict:
    """One site of each kind a poll card may point at."""
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.flush()
        point = Enterprise(
            enterprise_name="Завод А", branch_id=branch.id,
            active=True, enabled=True,
        )
        line = DpdLine(name="Лінія 1", branch_id=branch.id)
        session.add(point)
        session.add(line)
        await session.commit()
        return {"enterprise_id": point.id, "dpd_line_id": line.id}


async def add(**kwargs) -> int:
    async with async_session_factory() as session:
        card = PollDevice(**kwargs)
        session.add(card)
        await session.commit()
        return card.id


@pytest.mark.asyncio
class TestExactlyOneTarget:
    async def test_a_card_for_an_enterprise(self, targets):
        assert await add(enterprise_id=targets["enterprise_id"]) is not None

    async def test_a_card_for_a_dpd_line(self, targets):
        assert await add(dpd_line_id=targets["dpd_line_id"]) is not None

    async def test_a_card_pointing_nowhere_is_refused(self, targets):
        # A card with no target would be handed to an agent that then has
        # nothing to dial, and the failure would surface on the operator's
        # machine instead of here.
        with pytest.raises(IntegrityError):
            await add(phone="0501234567")

    async def test_a_card_pointing_at_both_kinds_is_refused(self, targets):
        # Where the readings land depends on the kind of site, so two of them
        # means two destinations for one poll.
        with pytest.raises(IntegrityError):
            await add(
                enterprise_id=targets["enterprise_id"],
                dpd_line_id=targets["dpd_line_id"],
            )


@pytest.mark.asyncio
class TestOneCardPerSite:
    async def test_a_second_card_for_the_same_site_is_refused(self, targets):
        await add(enterprise_id=targets["enterprise_id"])
        with pytest.raises(IntegrityError):
            await add(enterprise_id=targets["enterprise_id"])

    async def test_cards_of_different_kinds_do_not_collide(self, targets):
        # The unique indexes are partial: one of the two target columns is
        # always NULL, and a plain unique index would allow one such row in
        # the entire table.
        await add(enterprise_id=targets["enterprise_id"])
        assert await add(dpd_line_id=targets["dpd_line_id"]) is not None


@pytest.mark.asyncio
class TestHowASiteIsRead:
    """Both paths may be on; neither may be off. A site nobody reads is what
    `active` states deliberately — this would state it by accident."""

    async def test_a_site_starts_on_dpd(self, targets):
        async with async_session_factory() as session:
            point = await session.get(Enterprise, targets["enterprise_id"])
            assert (point.poll_dpd, point.poll_gsm) == (True, False)

    async def test_both_at_once_is_allowed(self, targets):
        async with async_session_factory() as session:
            point = await session.get(Enterprise, targets["enterprise_id"])
            point.poll_gsm = True
            await session.commit()

    async def test_neither_is_refused(self, targets):
        with pytest.raises(IntegrityError):
            async with async_session_factory() as session:
                point = await session.get(Enterprise, targets["enterprise_id"])
                point.poll_dpd = False
                await session.commit()

    async def test_the_same_holds_for_a_dpd_line(self, targets):
        with pytest.raises(IntegrityError):
            async with async_session_factory() as session:
                line = await session.get(DpdLine, targets["dpd_line_id"])
                line.poll_dpd = False
                await session.commit()


@pytest.mark.asyncio
class TestAssignment:
    async def test_removing_an_agent_frees_its_devices(self, targets):
        """Deleting an agent must not delete the devices it polled — only the
        fact that it was the one polling them."""
        card_id = await add(enterprise_id=targets["enterprise_id"])
        async with async_session_factory() as session:
            agent = PollAgent(name="АРМ", key_hash="x")
            session.add(agent)
            await session.flush()
            session.add(PollAgentDevice(agent_id=agent.id, poll_device_id=card_id))
            await session.commit()
            agent_id = agent.id

        async with async_session_factory() as session:
            await session.delete(await session.get(PollAgent, agent_id))
            await session.commit()

        async with async_session_factory() as session:
            assert await session.get(PollDevice, card_id) is not None
            assert await session.get(
                PollAgentDevice, (agent_id, card_id)
            ) is None
