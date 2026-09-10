"""When a device is due to be polled.

This function stands in for the whole scheduler that was not written: no jobs,
no queue, no assignment. Everything the feature promises about robustness comes
out of these rules, so they are pinned case by case.

The worked example from the plan: slots 08:00 and 16:00, the clock says 12:00,
the last poll was 08:15. The last slot to arrive is 08:00, the poll came after
it, so nothing happens. At 16:05 the last slot is 16:00, 08:15 < 16:00, and the
device is polled.
"""
from datetime import datetime

import pytest

from backend.services.poll_schedule import (
    REASON_ALREADY_POLLED,
    REASON_DISABLED,
    REASON_MANUAL_ONLY,
    REASON_MANUAL_REQUEST,
    REASON_NEVER_POLLED,
    REASON_NO_SLOTS,
    REASON_OVERDUE,
    is_due,
    last_slot,
)

TIMES = ["08:00", "16:00"]


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute)


def due(**over):
    base = dict(
        now=at(10, 12),
        poll_times=TIMES,
        default_times=["06:00"],
        last_poll_at=at(10, 8, 15),
        enabled=True,
        auto_poll=True,
        manual_requested_at=None,
    )
    return is_due(**{**base, **over})


class TestLastSlot:
    def test_the_most_recent_one_that_has_arrived(self):
        assert last_slot(at(10, 12), TIMES) == at(10, 8)
        assert last_slot(at(10, 16, 5), TIMES) == at(10, 16)

    def test_before_the_first_slot_it_is_yesterday_evening(self):
        # A device scheduled for 18:00 and looked at in the morning is not
        # "not yet scheduled" — it has been due since last night.
        assert last_slot(at(10, 3), ["18:00"]) == at(9, 18)

    def test_a_slot_exactly_now_counts_as_arrived(self):
        assert last_slot(at(10, 8), TIMES) == at(10, 8)

    def test_no_hours_at_all(self):
        assert last_slot(at(10, 12), []) is None
        assert last_slot(at(10, 12), None) is None

    def test_rubbish_entries_are_ignored_rather_than_crash(self):
        # The API refuses these on the way in; this is the belt to that braces,
        # because a bad row in the database must not stop the whole plan.
        assert last_slot(at(10, 12), ["25:00", "ранок", "08:00"]) == at(10, 8)


class TestTheWorkedExample:
    def test_polled_after_the_last_slot_means_nothing_to_do(self):
        assert due() == (False, REASON_ALREADY_POLLED)

    def test_after_the_next_slot_it_is_due_again(self):
        assert due(now=at(10, 16, 5)) == (True, REASON_OVERDUE)


class TestWhatSwitchesItOff:
    def test_a_disabled_card_is_never_due(self):
        assert due(enabled=False, last_poll_at=None) == (False, REASON_DISABLED)

    def test_without_a_schedule_it_waits_to_be_asked(self):
        assert due(auto_poll=False, last_poll_at=None) == (False, REASON_MANUAL_ONLY)

    def test_no_hours_anywhere_is_not_an_error(self):
        # A device kept for manual polling only, with the hours left empty.
        assert due(poll_times=[], last_poll_at=None) == (False, REASON_NO_SLOTS)


class TestManualRequests:
    def test_a_request_jumps_the_queue(self):
        assert due(manual_requested_at=at(10, 11)) == (True, REASON_MANUAL_REQUEST)

    def test_it_outranks_a_switched_off_schedule(self):
        # Turning the schedule off says something about the schedule, not that
        # the device may never be read when somebody asks.
        assert due(auto_poll=False, manual_requested_at=at(10, 11)) == (
            True, REASON_MANUAL_REQUEST,
        )

    def test_but_not_a_disabled_card(self):
        assert due(enabled=False, manual_requested_at=at(10, 11)) == (
            False, REASON_DISABLED,
        )

    def test_a_request_older_than_the_last_poll_is_already_served(self):
        # The poll that followed it did the job; without this the device would
        # be polled forever.
        assert due(manual_requested_at=at(10, 7)) == (False, REASON_ALREADY_POLLED)


class TestFirstPollAndRecovery:
    def test_a_device_never_polled_is_due(self):
        assert due(last_poll_at=None) == (True, REASON_NEVER_POLLED)

    def test_a_missing_agent_loses_nothing(self):
        """The reason there is no queue.

        Іваненко's PC is off at 08:00, so nobody calls. An hour later
        Петренко's agent — which has the same device ticked — runs the same
        arithmetic, sees a poll older than the 08:00 slot, and polls it.
        """
        assert due(now=at(10, 9), last_poll_at=at(9, 16, 10)) == (
            True, REASON_OVERDUE,
        )

    def test_and_the_second_agent_finds_nothing_to_do_afterwards(self):
        # Whoever got there first wrote last_poll_at, and the other one sees it.
        assert due(now=at(10, 9), last_poll_at=at(10, 8, 30)) == (
            False, REASON_ALREADY_POLLED,
        )


class TestOwnHoursVersusGlobal:
    def test_none_means_follow_the_global_hours(self):
        # Global 06:00, clock 12:00, last poll yesterday → due.
        assert due(poll_times=None, last_poll_at=at(9, 20)) == (True, REASON_OVERDUE)

    def test_and_its_own_hours_win_when_it_has_them(self):
        # Same moment, but this device only wants 16:00: 08:00 has passed and
        # the poll at 08:15 covered it.
        assert due(poll_times=TIMES) == (False, REASON_ALREADY_POLLED)

    @pytest.mark.parametrize("times", [["23:59"], ["00:00"]])
    def test_the_edges_of_the_day(self, times):
        assert due(poll_times=times, last_poll_at=None)[0] is True
