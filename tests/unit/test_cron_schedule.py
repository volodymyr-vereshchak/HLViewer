"""The schedule, said in cron: what it accepts and which slot has passed."""
from datetime import datetime

import pytest

from backend.services import cron_schedule as cron


class TestParsing:
    def test_the_shapes_a_poll_is_written_in(self):
        for expression in ("0 8 * * *", "0 */4 * * *", "0 * * * *",
                           "30 8,20 * * *", "0 6-18/2 * * mon-fri"):
            assert cron.parse(expression)

    def test_sunday_is_the_same_day_written_either_way(self):
        assert cron.parse("0 8 * * 7")["weekday"] == {0}
        assert cron.parse("0 8 * * sun")["weekday"] == {0}

    @pytest.mark.parametrize("expression, says", [
        ("", "Порожній"),
        ("0 8 * *", "п'ять полів"),
        ("0 25 * * *", "поза межами"),
        ("0 8 * * blah", "Не розумію"),
        ("0 18-6 * * *", "навпаки"),
    ])
    def test_an_expression_nobody_could_have_meant_is_refused(self, expression, says):
        with pytest.raises(cron.CronError) as refusal:
            cron.parse(expression)
        assert says in str(refusal.value)


class TestLastSlot:
    def test_the_hour_that_has_just_passed(self):
        now = datetime(2026, 9, 20, 14, 37)
        assert cron.last_fire(now, "0 * * * *") == datetime(2026, 9, 20, 14, 0)

    def test_the_slot_of_a_once_a_day_poll_before_it_comes_round(self):
        # Looked at in the morning, a site polled at 18:00 is overdue since
        # yesterday evening — not "not scheduled yet".
        now = datetime(2026, 9, 20, 7, 0)
        assert cron.last_fire(now, "0 18 * * *") == datetime(2026, 9, 19, 18, 0)

    def test_a_slot_that_has_not_arrived_today_is_yesterday(self):
        now = datetime(2026, 9, 20, 7, 59)
        assert cron.last_fire(now, "0 8,20 * * *") == datetime(2026, 9, 19, 20, 0)

    def test_every_four_hours(self):
        now = datetime(2026, 9, 20, 13, 5)
        assert cron.last_fire(now, "0 */4 * * *") == datetime(2026, 9, 20, 12, 0)

    def test_a_weekday_only_schedule_reaches_back_over_the_weekend(self):
        # Monday 20.09.2026 is a Sunday, so Friday is the last weekday slot.
        now = datetime(2026, 9, 20, 9, 0)          # Sunday
        assert cron.last_fire(now, "0 8 * * mon-fri") == datetime(2026, 9, 18, 8, 0)

    def test_a_schedule_that_has_not_fired_within_the_window_says_nothing(self):
        # The 29th of February, looked at in September.
        assert cron.last_fire(datetime(2026, 9, 20, 9, 0), "0 8 29 2 *") is None

    def test_a_broken_expression_names_no_slot(self):
        assert cron.last_fire(datetime(2026, 9, 20, 9, 0), "не розклад") is None


class TestWords:
    @pytest.mark.parametrize("expression, words", [
        ("0 * * * *", "щогодини"),
        ("0 */4 * * *", "кожні 4 год"),
        ("0 8 * * *", "щодня о 08:00"),
        ("0 8,20 * * *", "о 08:00, 20:00"),
        ("0 8 * * mon", "за власним розкладом"),
    ])
    def test_said_in_words_for_the_line_under_the_field(self, expression, words):
        assert cron.describe(expression) == words


class TestFromTheOldList:
    def test_hours_become_the_cron_that_means_the_same(self):
        assert cron.from_times(["08:00", "20:00"]) == "0 8,20 * * *"
        assert cron.from_times(["06:30"]) == "30 6 * * *"

    def test_nothing_set_stays_nothing(self):
        assert cron.from_times([]) is None
        assert cron.from_times(None) is None

    def test_the_slot_it_names_is_the_slot_the_list_named(self):
        now = datetime(2026, 9, 20, 14, 37)
        assert cron.last_fire(now, cron.from_times(["08:00", "20:00"])) == (
            datetime(2026, 9, 20, 8, 0)
        )
