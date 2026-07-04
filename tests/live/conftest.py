"""Live-level conftest: everything here talks to real external services
(DPD API). Excluded from the default run via `-m "not live"` in pyproject."""

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "/tests/live/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.live)
