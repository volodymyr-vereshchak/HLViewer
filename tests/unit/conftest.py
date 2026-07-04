"""Unit-level conftest: tags everything in this package with the `unit` marker.
Unit tests must not touch the database or the network."""

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "/tests/unit/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.unit)
