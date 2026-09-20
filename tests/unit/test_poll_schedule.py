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
    REASON_COOLING_OFF,
    REASON_GAVE_UP,
    REASON_DISABLED,
    REASON_MANUAL_ONLY,
    REASON_MANUAL_REQUEST,
    REASON_NEVER_POLLED,
    REASON_NO_SLOTS,
    REASON_OVERDUE,
    is_due,
    last_slot,
)

CRON = "0 8,16 * * *"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute)


def due(**over):
    base = dict(
        now=at(10, 12),
        poll_cron=CRON,
        default_cron="0 6 * * *",
        last_poll_at=at(10, 8, 15),
        enabled=True,
        auto_poll=True,
        manual_requested_at=None,
        last_attempt_at=None,
        last_status=None,
        scheduled_failures=None,
    )
    return is_due(**{**base, **over})


class TestLastSlot:
    def test_the_most_recent_one_that_has_arrived(self):
        assert last_slot(at(10, 12), CRON) == at(10, 8)
        assert last_slot(at(10, 16, 5), CRON) == at(10, 16)

    def test_before_the_first_slot_it_is_yesterday_evening(self):
        # A device scheduled for 18:00 and looked at in the morning is not
        # "not yet scheduled" — it has been due since last night.
        assert last_slot(at(10, 3), "0 18 * * *") == at(9, 18)

    def test_a_slot_exactly_now_counts_as_arrived(self):
        assert last_slot(at(10, 8), CRON) == at(10, 8)

    def test_no_schedule_at_all(self):
        assert last_slot(at(10, 12), "") is None
        assert last_slot(at(10, 12), None) is None

    def test_a_broken_expression_names_no_slot_rather_than_crashing(self):
        # The API refuses these on the way in; this is the belt to that braces,
        # because a bad row in the database must not stop the whole plan.
        assert last_slot(at(10, 12), "щоранку") is None


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

    def test_no_schedule_anywhere_is_not_an_error(self):
        # A device kept for manual polling only, with the schedule left empty.
        assert due(poll_cron=None, default_cron=None,
                   last_poll_at=None) == (False, REASON_NO_SLOTS)


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
    def test_none_means_follow_the_global_schedule(self):
        # Global 06:00, clock 12:00, last poll yesterday → due.
        assert due(poll_cron=None, last_poll_at=at(9, 20)) == (True, REASON_OVERDUE)

    def test_and_its_own_schedule_wins_when_it_has_one(self):
        # Same moment, but this device only wants 08:00 and 16:00: 08:00 has
        # passed and the poll at 08:15 covered it.
        assert due(poll_cron=CRON) == (False, REASON_ALREADY_POLLED)

    @pytest.mark.parametrize("schedule", ["59 23 * * *", "0 0 * * *"])
    def test_the_edges_of_the_day(self, schedule):
        assert due(poll_cron=schedule, last_poll_at=None)[0] is True

    def test_every_hour_is_five_characters_now(self):
        # What the list could not say without twenty-four entries.
        assert due(poll_cron="0 * * * *", last_poll_at=at(10, 11, 30)) == (
            True, REASON_OVERDUE
        )


class TestAfterAFailure:
    """A meter that did not answer is not asked again a moment later.

    Without the pause, a device that cannot be reached is redialled as fast as
    agents fetch their plan — every fifteen seconds, three dial attempts each —
    for as long as its slot stays unsatisfied. It buys nothing: a line that was
    dead a minute ago is dead now, and the modem is held busy for every other
    site meanwhile.
    """

    def test_a_failed_attempt_is_not_retried_at_once(self):
        assert due(
            last_poll_at=at(10, 7),          # older than the 08:00 slot: overdue
            last_attempt_at=at(10, 11, 58),
            last_status="error",
        ) == (False, REASON_COOLING_OFF)

    def test_it_is_retried_once_the_pause_is_over(self):
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 11, 40),
            last_status="error",
        ) == (True, REASON_OVERDUE)

    def test_a_person_asking_again_is_not_made_to_wait(self):
        # They have just watched it fail and pressed the button anyway. That
        # is a decision, not a retry storm.
        assert due(
            last_attempt_at=at(10, 11, 58),
            last_status="error",
            manual_requested_at=at(10, 11, 59),
        ) == (True, REASON_MANUAL_REQUEST)

    def test_a_successful_poll_is_never_a_reason_to_wait(self):
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 11, 58),
            last_status="ok",
        ) == (True, REASON_OVERDUE)


class TestGivingUpUntilTheNextSlot:
    """Three calls to a line that is not answering, then quiet.

    A quarter of an hour apart, three attempts cover what a retry can fix: a
    busy line, a meter mid-something, a modem that had not come back yet. The
    fourth would be the first of a hundred a day, with the modem unavailable
    to every other site meanwhile.
    """

    def test_a_third_failure_ends_the_attempts_for_this_slot(self):
        failures = [at(10, 8, 5), at(10, 8, 20), at(10, 8, 35)]
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 8, 35),
            last_status="error",
            scheduled_failures=failures,
            now=at(10, 9),
        ) == (False, REASON_GAVE_UP)

    def test_two_failures_still_leave_one_call(self):
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 8, 20),
            last_status="error",
            scheduled_failures=[at(10, 8, 5), at(10, 8, 20)],
            now=at(10, 9),
        ) == (True, REASON_OVERDUE)

    def test_the_next_slot_starts_over(self):
        # Failed all morning; 16:00 arrives and the site is tried again. The
        # count is per slot, not a running total, or a site that fails every
        # morning would never be polled again.
        failures = [at(10, 8, 5), at(10, 8, 20), at(10, 8, 35)]
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 8, 35),
            last_status="error",
            scheduled_failures=failures,
            now=at(10, 16, 30),
        ) == (True, REASON_OVERDUE)

    def test_a_person_can_still_ask(self):
        failures = [at(10, 8, 5), at(10, 8, 20), at(10, 8, 35)]
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 8, 35),
            last_status="error",
            scheduled_failures=failures,
            manual_requested_at=at(10, 9),
            now=at(10, 9),
        ) == (True, REASON_MANUAL_REQUEST)


class TestAManualPollDuringThePause:
    """Somebody polls by hand while the automatic retry is still waiting.

    Both outcomes have to be right, because this is what an operator actually
    does after seeing a failure on the screen.
    """

    def test_a_successful_manual_poll_closes_the_slot(self):
        # Read at 08:10 by hand, after the 08:00 slot: there is nothing left
        # for the pending retry to fetch, so it simply does not happen.
        assert due(
            last_poll_at=at(10, 8, 10),
            last_attempt_at=at(10, 8, 10),
            last_status="ok",
            scheduled_failures=[at(10, 8, 2)],
            now=at(10, 8, 20),
        ) == (False, REASON_ALREADY_POLLED)

    def test_a_failed_manual_poll_restarts_the_pause(self):
        # It was an attempt like any other: the next automatic one is fifteen
        # minutes from THIS call, not from the earlier one.
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 8, 10),
            last_status="error",
            scheduled_failures=[at(10, 8, 10), at(10, 8, 2)],
            now=at(10, 8, 20),
        ) == (False, REASON_COOLING_OFF)

    def test_a_failed_manual_poll_does_not_count_towards_the_three(self):
        # Two scheduled failures and one by hand. The hand-made call is not in
        # the list at all — the schedule still has its third attempt, because
        # looking at a site that is failing must not remove its retries.
        assert due(
            last_poll_at=at(10, 7),
            last_attempt_at=at(10, 8, 30),
            last_status="error",
            scheduled_failures=[at(10, 8, 15), at(10, 8, 2)],
            now=at(10, 8, 50),
        ) == (True, REASON_OVERDUE)
