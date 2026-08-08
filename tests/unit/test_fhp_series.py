"""ФХП series: decoding, step functions, time weighting, deviations.

Pure functions, no DB. These numbers are the feature — the endpoint and the
frontend only move them around — so they are worked out by hand here.
"""

from datetime import date, datetime, timedelta

import pytest

from backend.services import commercial_day, fhp_series
from backend.services.edit_value_codec import (
    PLAUSIBLE,
    decode_float,
    encode_float,
    is_plausible,
)
from backend.services.fhp_series import (
    Deviation,
    build_steps,
    daily_series,
    deviations,
    hourly_series,
    line_stats,
    reference_series,
    seed_value,
    spread_series,
    staleness,
)


def dt(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 5, day, hour, minute)


class TestCodec:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (1061103547, 0.7467),   # густина, кг/м³
            (1058860433, 0.6130),   # CO2, мол.%
            (1073360981, 1.9546),   # N2, мол.%
        ],
    )
    def test_decodes_values_seen_in_the_archive(self, raw, expected):
        assert decode_float(raw) == pytest.approx(expected, rel=1e-4)

    def test_nan_and_inf_are_not_values(self):
        assert decode_float(0x7FC00000 - (1 << 32)) is None  # NaN as signed
        assert decode_float(0x7F800000) is None              # +Inf

    def test_small_int_band_is_a_denormal_not_an_integer(self):
        # The frontend renders |raw| <= 32767 as the integer itself; for ФХП we
        # deliberately do not. If this ever stops being ~0 the two decoders have
        # drifted and the same archive row would read differently in the two
        # screens.
        assert decode_float(1) == pytest.approx(0.0, abs=1e-38)
        assert not is_plausible(1, decode_float(1))

    def test_round_trip(self):
        for value in (0.7467, 0.613, 1.9546, 0.0):
            assert decode_float(encode_float(value)) == pytest.approx(value, rel=1e-6)

    @pytest.mark.parametrize("edit_type_id", sorted(PLAUSIBLE))
    def test_band_edges_are_inclusive(self, edit_type_id):
        low, high = PLAUSIBLE[edit_type_id]
        assert is_plausible(edit_type_id, low)
        assert is_plausible(edit_type_id, high)
        assert not is_plausible(edit_type_id, low - 0.001)
        assert not is_plausible(edit_type_id, high + 0.001)

    def test_garbage_is_rejected(self):
        assert not is_plausible(1, decode_float(encode_float(1e30)))


class TestSeedValue:
    def test_prior_change_wins(self):
        assert seed_value(0.7460, 0.7000) == 0.7460

    def test_falls_back_to_what_the_first_in_range_change_replaced(self):
        assert seed_value(None, 0.7000) == 0.7000

    def test_nothing_known_means_no_series(self):
        assert seed_value(None, None) is None


class TestBuildSteps:
    def test_seed_keeps_its_own_instant(self):
        steps = build_steps([], 0.74, dt(1, 7), seed_at=dt(1, 7) - timedelta(days=3))
        assert steps == [(datetime(2026, 4, 28, 7), 0.74)]

    def test_seed_later_than_range_start_is_pinned_to_the_range(self):
        # A seed can never start after the range does; that would leave the
        # first hours uncovered for no reason.
        steps = build_steps([], 0.74, dt(1, 7), seed_at=dt(2, 0))
        assert steps[0][0] == dt(1, 7)

    def test_same_instant_the_later_row_wins(self):
        steps = build_steps(
            [(dt(1, 8), 0.75), (dt(1, 8), 0.76)], 0.74, dt(1, 7)
        )
        assert steps == [(dt(1, 7), 0.74), (dt(1, 8), 0.76)]

    def test_backwards_stamp_is_dropped(self):
        # Autumn DST: 02:xx repeats, so a later row can carry an earlier stamp.
        steps = build_steps(
            [(dt(1, 9), 0.75), (dt(1, 8), 0.76), (dt(1, 10), 0.77)], 0.74, dt(1, 7)
        )
        assert [s[0] for s in steps] == [dt(1, 7), dt(1, 9), dt(1, 10)]

    def test_rows_before_the_range_are_not_steps(self):
        steps = build_steps([(dt(1, 3), 0.70), (dt(1, 8), 0.75)], 0.74, dt(1, 7))
        assert [s[0] for s in steps] == [dt(1, 7), dt(1, 8)]

    def test_no_seed_starts_at_the_first_change(self):
        steps = build_steps([(dt(1, 8), 0.75)], None, dt(1, 7))
        assert steps == [(dt(1, 8), 0.75)]


class TestHourlySeries:
    def test_one_value_all_hour(self):
        steps = [(dt(1, 7), 0.7467)]
        series = hourly_series(steps, dt(1, 7), dt(1, 8))
        assert set(series) == {dt(1, 7)}
        assert series[dt(1, 7)] == pytest.approx(0.7467)

    def test_change_at_half_past_is_the_midpoint(self):
        steps = [(dt(1, 7), 0.7), (dt(1, 7, 30), 0.8)]
        series = hourly_series(steps, dt(1, 7), dt(1, 8))
        assert series[dt(1, 7)] == pytest.approx(0.75)

    def test_three_values_weighted_by_their_minutes(self):
        steps = [(dt(1, 7), 0.70), (dt(1, 7, 15), 0.80), (dt(1, 7, 45), 0.90)]
        series = hourly_series(steps, dt(1, 7), dt(1, 8))
        expected = (0.70 * 900 + 0.80 * 1800 + 0.90 * 900) / 3600
        assert series[dt(1, 7)] == pytest.approx(expected)

    def test_change_on_the_boundary_does_not_touch_the_previous_hour(self):
        steps = [(dt(1, 7), 0.70), (dt(1, 8), 0.90)]
        series = hourly_series(steps, dt(1, 7), dt(1, 9))
        assert series[dt(1, 7)] == pytest.approx(0.70)
        assert series[dt(1, 8)] == pytest.approx(0.90)

    def test_hours_before_the_series_are_absent(self):
        steps = [(dt(1, 9), 0.70)]
        series = hourly_series(steps, dt(1, 7), dt(1, 11))
        assert dt(1, 7) not in series and dt(1, 8) not in series
        assert set(series) == {dt(1, 9), dt(1, 10)}

    def test_real_cadence_mixes_in_the_right_proportion(self):
        # Как в живых данных: изменения в 06:21, 07:25, 08:23.
        steps = [(dt(1, 6, 21), 0.7460), (dt(1, 7, 25), 0.7467), (dt(1, 8, 23), 0.7469)]
        series = hourly_series(steps, dt(1, 6), dt(1, 9))
        expected = (0.7460 * 25 * 60 + 0.7467 * 35 * 60) / 3600
        assert series[dt(1, 7)] == pytest.approx(expected)

    def test_empty_steps_give_an_empty_series(self):
        assert hourly_series([], dt(1, 7), dt(2, 7)) == {}


class TestDailySeries:
    def test_plain_mean_of_the_hours(self):
        hours = commercial_day.hours_of_day(date(2026, 5, 1), 7)
        hourly = {h: 0.70 + i * 0.01 for i, h in enumerate(hours)}
        result = daily_series(hourly, [date(2026, 5, 1)], 7)
        mean, present = result[date(2026, 5, 1)]
        assert present == 24
        assert mean == pytest.approx(sum(hourly.values()) / 24)

    def test_is_not_the_same_as_weighting_the_whole_day(self):
        # 23 hours at 0.70 and one at 0.90: the mean of the hourly means is
        # 0.7083, while time-weighting the day as one span gives the same only
        # because every hour is equally long — so make the hours UNEQUAL in
        # coverage instead: one hour known from 15 minutes only.
        hours = commercial_day.hours_of_day(date(2026, 5, 1), 7)
        hourly = {h: 0.70 for h in hours[:-1]}
        hourly[hours[-1]] = 0.90  # this hour is one quarter as long in reality
        mean, present = daily_series(hourly, [date(2026, 5, 1)], 7)[date(2026, 5, 1)]
        assert present == 24
        assert mean == pytest.approx((0.70 * 23 + 0.90) / 24)
        # Time-weighting 23h at 0.70 and 15min at 0.90 would give ~0.7021.
        assert mean != pytest.approx((0.70 * 23 * 3600 + 0.90 * 900) / (23 * 3600 + 900))

    def test_missing_hours_divide_by_the_hours_present(self):
        hours = commercial_day.hours_of_day(date(2026, 5, 1), 7)
        hourly = {h: 0.70 for h in hours}
        del hourly[hours[3]]  # spring forward: this local hour never happened
        mean, present = daily_series(hourly, [date(2026, 5, 1)], 7)[date(2026, 5, 1)]
        assert present == 23
        assert mean == pytest.approx(0.70)

    def test_a_day_with_no_hours_is_absent(self):
        assert daily_series({}, [date(2026, 5, 1)], 7) == {}


class TestCommercialDay:
    def test_day_bounds_end_is_exclusive(self):
        start, end = commercial_day.day_bounds(date(2026, 5, 1), 7)
        assert start == dt(1, 7)
        assert end == dt(2, 7)

    def test_day_of_before_contract_hour_is_the_previous_day(self):
        assert commercial_day.day_of(dt(2, 6, 59), 7) == date(2026, 5, 1)
        assert commercial_day.day_of(dt(2, 7), 7) == date(2026, 5, 2)

    def test_contract_hour_zero_is_the_calendar_day(self):
        assert commercial_day.day_of(dt(2, 0), 0) == date(2026, 5, 2)
        start, end = commercial_day.day_bounds(date(2026, 5, 2), 0)
        assert (start, end) == (dt(2, 0), dt(3, 0))

    def test_range_window_spans_whole_commercial_days(self):
        start, end = commercial_day.range_window(date(2026, 5, 1), date(2026, 5, 3), 7)
        assert (start, end) == (dt(1, 7), dt(4, 7))

    def test_hours_of_day_is_24_slots_from_the_contract_hour(self):
        hours = commercial_day.hours_of_day(date(2026, 5, 1), 7)
        assert len(hours) == 24
        assert hours[0] == dt(1, 7) and hours[-1] == dt(2, 6)


class TestReferenceSeries:
    def test_mean_of_the_references_present(self):
        per_line = {1: {dt(1, 7): 0.74, dt(1, 8): 0.76}, 2: {dt(1, 7): 0.76}}
        ref, counts = reference_series(per_line, [1, 2])
        assert ref[dt(1, 7)] == pytest.approx(0.75)
        assert counts[dt(1, 7)] == 2

    def test_a_silent_reference_leaves_the_other_alone(self):
        per_line = {1: {dt(1, 8): 0.76}, 2: {dt(1, 7): 0.74}}
        ref, counts = reference_series(per_line, [1, 2])
        assert ref[dt(1, 8)] == pytest.approx(0.76)
        assert counts[dt(1, 8)] == 1

    def test_no_reference_has_a_value_that_period(self):
        ref, _ = reference_series({1: {}}, [1])
        assert ref == {}


class TestDeviations:
    def test_only_periods_both_sides_have(self):
        devs = deviations({dt(1, 7): 0.74, dt(1, 8): 0.75}, {dt(1, 7): 0.75})
        assert [d.period for d in devs] == [dt(1, 7)]
        assert devs[0].delta == pytest.approx(-0.01)
        assert devs[0].delta_pct == pytest.approx(-0.01 / 0.75 * 100)

    def test_zero_reference_has_no_percentage(self):
        devs = deviations({dt(1, 7): 0.74}, {dt(1, 7): 0.0})
        assert devs[0].delta_pct is None


class TestLineStats:
    def devs(self, *deltas) -> list[Deviation]:
        return [
            Deviation(dt(1, 7 + i), 1.0 + d, 1.0, d, d * 100.0)
            for i, d in enumerate(deltas)
        ]

    def test_signed_and_absolute_means_differ(self):
        stats = line_stats(self.devs(0.02, -0.04), tolerance=0.10)
        assert stats.mean_delta == pytest.approx(-0.01)
        assert stats.mean_abs_delta == pytest.approx(0.03)

    def test_max_takes_the_first_of_a_tie(self):
        stats = line_stats(self.devs(0.05, -0.05), tolerance=1.0)
        assert stats.max_abs_delta == pytest.approx(0.05)
        assert stats.max_abs_delta_at == dt(1, 7)

    def test_absolute_tolerance(self):
        stats = line_stats(self.devs(0.01, 0.05, -0.09), tolerance=0.04)
        assert stats.out_of_tolerance == 2
        assert stats.out_of_tolerance_share == pytest.approx(200 / 3)

    def test_percentage_tolerance(self):
        # delta_pct is delta*100 in this fixture, so 0.05 → 5 %.
        stats = line_stats(self.devs(0.01, 0.05, -0.09), tolerance=4.0, mode="pct")
        assert stats.out_of_tolerance == 2

    def test_empty_has_no_statistics(self):
        assert line_stats([], tolerance=0.01) is None


class TestSpreadSeries:
    def test_min_max_and_how_many_lines(self):
        per_line = {
            1: {dt(1, 7): 0.74},
            2: {dt(1, 7): 0.76, dt(1, 8): 0.75},
            3: {},
        }
        spread = spread_series(per_line)
        assert spread[dt(1, 7)] == (
            pytest.approx(0.74), pytest.approx(0.76), pytest.approx(0.02), 2
        )
        assert spread[dt(1, 8)][3] == 1


class TestStaleness:
    def test_older_than_the_limit_is_flagged(self):
        steps = [(dt(1, 7), 0.74)]
        hours = [dt(1, 7) + timedelta(hours=h) for h in (47, 48, 49, 60)]
        stale = staleness(steps, hours, max_age_hours=48)
        assert dt(1, 7) + timedelta(hours=47) not in stale
        assert dt(1, 7) + timedelta(hours=48) not in stale
        assert dt(1, 7) + timedelta(hours=49) in stale
        assert dt(1, 7) + timedelta(hours=60) in stale

    def test_a_fresh_change_clears_it(self):
        steps = [(dt(1, 7), 0.74), (dt(3, 7), 0.75)]
        stale = staleness(steps, [dt(3, 8)], max_age_hours=48)
        assert stale == set()

    def test_no_steps_flags_nothing(self):
        assert staleness([], [dt(1, 7)], max_age_hours=48) == set()
