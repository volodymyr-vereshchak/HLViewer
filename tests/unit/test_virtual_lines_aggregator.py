"""Rolling physical lines up into virtual (ring) lines: volumes are summed,
pressure/temperature/density are volume-weighted, lines outside any requested
ring pass through untouched."""

from datetime import datetime
from unittest.mock import patch

import pytest

from backend.db.models.hourly_archive_model import HourlyArchiveList
from backend.services import virtual_lines_aggregator

RINGS = {
    "1001": {"name": "Ring North", "physical_line_ids": [1, 6, 22], "active": True},
    "1002": {"name": "Ring South", "physical_line_ids": [4, 10], "active": True},
}
H0 = datetime(2025, 12, 1, 0)
H1 = datetime(2025, 12, 1, 1)


def rec(line_id, volume, period=H0, pressure=5.0, temperature=20.0, density=0.8):
    return HourlyArchiveList(
        id=line_id, line_id=line_id, period=period, volume=volume,
        w_volume_dp=volume - 5, pressure=pressure, temperature=temperature,
        density=density,
    )


@pytest.fixture(autouse=True)
def _rings():
    with patch.object(virtual_lines_aggregator, "get_active_virtual_lines",
                      return_value=RINGS):
        yield


def aggregate(archives, requested):
    return virtual_lines_aggregator.aggregate_to_virtual_lines(
        archives, requested_line_ids=requested)


def test_volumes_sum_and_the_rest_is_volume_weighted():
    [row] = aggregate([
        rec(1, 100.0, pressure=5.0, temperature=20.0, density=0.80),
        rec(6, 200.0, pressure=5.5, temperature=22.0, density=0.82),
        rec(22, 150.0, pressure=5.2, temperature=21.0, density=0.81),
    ], [1001])
    assert (row["line_id"], row["period"]) == (1001, H0)
    assert row["volume"] == 450.0
    assert row["w_volume_dp"] == 435.0
    assert row["pressure"] == pytest.approx(2380 / 450)
    assert row["temperature"] == pytest.approx(9550 / 450)
    assert row["density"] == pytest.approx(365.5 / 450)


def test_one_row_per_ring_and_period_physical_lines_pass_through():
    rows = aggregate([
        rec(1, 100.0), rec(6, 200.0),
        rec(1, 110.0, period=H1), rec(6, 210.0, period=H1),
        rec(4, 150.0), rec(10, 180.0),
        rec(25, 300.0),
    ], [1001, 1002, 25])
    got = {(r["line_id"], r["period"]): r["volume"] for r in rows}
    assert got == {
        (1001, H0): 300.0, (1001, H1): 320.0,
        (1002, H0): 330.0,
        (25, H0): 300.0,
    }


def test_zero_volume_gives_zeros_not_a_division_error():
    [row] = aggregate([rec(1, 0.0), rec(6, 0.0)], [1001])
    assert (row["volume"], row["pressure"], row["temperature"], row["density"]) == (
        0.0, 0.0, 0.0, 0.0)
