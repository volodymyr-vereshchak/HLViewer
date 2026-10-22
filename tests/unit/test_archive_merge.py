"""Folding records that share a period inside one hostlib file."""
from datetime import datetime

import pytest

from backend.hl_engine.archive_merge import merge_same_period
from backend.hl_engine.hl_engine import _file_order

MARKER = 1.401298464324817e-45  # the integer 1 read as float32: "no data"
H3 = datetime(2024, 10, 27, 3)


def rec(volume, pressure=37.3, temperature=20.0, period=H3, **extra):
    return {
        "period": period, "line_id": 1, "volume": volume, "w_volume_dp": 0.0,
        "pressure": pressure, "temperature": temperature, "density": 0.76,
        **extra,
    }


def test_single_records_pass_through_untouched():
    records = [rec(1.0, period=datetime(2024, 10, 27, h)) for h in range(3)]
    assert merge_same_period(records) is records


def test_clock_back_hour_is_summed_and_averaged():
    """«Побут 1», 27.10.2024: two 03:00 records, each a real hour."""
    [merged] = merge_same_period([
        rec(7395.73, pressure=37.286, temperature=19.73, w_volume_dp=76.3),
        rec(7383.11, pressure=37.291, temperature=20.05, w_volume_dp=76.2),
    ])
    assert merged["volume"] == pytest.approx(14778.84)
    assert merged["w_volume_dp"] == pytest.approx(152.5)
    assert merged["pressure"] == pytest.approx(37.2885)
    assert merged["temperature"] == pytest.approx(19.89)
    assert merged["period"] == H3


def test_empty_record_next_to_a_real_one_is_dropped():
    """«ВП ДЕЛЬТА», 06.12.2024 after a power failure: averaging the empty
    record in would have halved the pressure."""
    [merged] = merge_same_period([
        rec(0.0, pressure=0.89, temperature=MARKER),
        rec(1199.41, pressure=6.131, temperature=3.295),
    ])
    assert merged["volume"] == pytest.approx(1199.41)
    assert merged["pressure"] == pytest.approx(6.131)
    assert merged["temperature"] == pytest.approx(3.295)


def test_marker_volume_counts_as_no_gas():
    [merged] = merge_same_period([rec(MARKER), rec(100.0)])
    assert merged["volume"] == pytest.approx(100.0)


def test_all_empty_keeps_the_measured_ones():
    [merged] = merge_same_period([
        rec(0.0, pressure=1.02, temperature=13.7),
        rec(0.0, pressure=MARKER, temperature=MARKER),
        rec(0.0, pressure=1.04, temperature=13.9),
    ])
    assert merged["volume"] == 0.0
    assert merged["pressure"] == pytest.approx(1.03)
    assert merged["temperature"] == pytest.approx(13.8)


def test_all_empty_and_unmeasured_keeps_the_last():
    last = rec(0.0, pressure=MARKER, temperature=0.0, density=0.5)
    [merged] = merge_same_period([rec(0.0, pressure=MARKER, temperature=MARKER), last])
    assert merged == last


def test_order_of_periods_is_kept():
    h = lambda n: datetime(2024, 10, 27, n)
    merged = merge_same_period([
        rec(1.0, period=h(2)), rec(2.0, period=h(3)), rec(3.0, period=h(3)),
        rec(4.0, period=h(4)),
    ])
    assert [r["period"] for r in merged] == [h(2), h(3), h(4)]
    assert [r["volume"] for r in merged] == [1.0, 5.0, 4.0]


def test_monthly_files_sort_in_calendar_order():
    names = ["S100R2D.126", "S100R2D.C25", "S100R2D.A26", "S100R2D.926", "S100R2D.B25"]
    ordered = sorted((f"d/{n}" for n in names), key=_file_order)
    assert [p[2:] for p in ordered] == [
        "S100R2D.B25", "S100R2D.C25", "S100R2D.126", "S100R2D.926", "S100R2D.A26",
    ]
