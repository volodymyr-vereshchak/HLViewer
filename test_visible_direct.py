import asyncio
from backend.db.engine import async_session_factory
from backend.db.dao.line_dao import LineDao
from backend.services.virtual_lines_config import get_active_virtual_lines, get_physical_lines_in_rings

async def test():
    # Test 1: Get virtual lines
    print("Test 1: get_active_virtual_lines()")
    virtual_lines = get_active_virtual_lines()
    print(f"  Result: {len(virtual_lines)} virtual lines")

    # Test 2: Get physical lines in rings
    print("\nTest 2: get_physical_lines_in_rings()")
    physical_in_rings = get_physical_lines_in_rings()
    print(f"  Result: {physical_in_rings}")

    # Test 3: Get all physical lines from DB
    print("\nTest 3: LineDao.get_all()")
    async with async_session_factory() as session:
        dao = LineDao(session=session)
        print(f"  DAO created successfully")
        all_physical_lines = await dao.get_all()
        print(f"  Result: {len(all_physical_lines)} lines")

        # Test 4: Filter out lines in rings
        print("\nTest 4: Filter visible lines")
        visible = [line for line in all_physical_lines if line.id not in physical_in_rings]
        print(f"  Result: {len(visible)} visible physical lines (out of {len(all_physical_lines)} total)")

asyncio.run(test())
