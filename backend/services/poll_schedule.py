"""Whether a device is due to be polled.

This one function replaces everything a scheduler would have been: there are no
poll jobs, no queue and no assignment. A device is due when the last slot that
has passed is later than the last successful poll, and that is arithmetic over
data the admin panel already holds.

Two consequences worth stating, because they are why it was built this way:

  * **A missing agent loses nothing.** If the machine that usually polls a
    device is switched off at 08:00, the device stays overdue and the next
    agent to look — an hour later, a different operator — sees exactly the same
    arithmetic and polls it. Nothing has to be re-assigned, because nothing was
    assigned.
  * **The server decides, not the agent.** Workstation clocks drift, and a PC
    an hour fast would either poll for nothing or miss its slot. The plan
    endpoint answers with `due` already computed, so every agent agrees.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

# What `due` says when it says no. Returned alongside the flag because the
# admin screen shows it, and "not due" alone tells an operator nothing about
# whether the setup is right.
REASON_DISABLED = "disabled"
REASON_MANUAL_ONLY = "manual_only"
REASON_NO_SLOTS = "no_slots"
REASON_ALREADY_POLLED = "already_polled"
REASON_MANUAL_REQUEST = "manual_request"
REASON_NEVER_POLLED = "never_polled"
REASON_OVERDUE = "overdue"
REASON_COOLING_OFF = "cooling_off"
REASON_GAVE_UP = "gave_up"

#: How long a device is left alone after a failed attempt.
#:
#: Without it a device that cannot be reached is redialled as fast as agents
#: ask for their plan — every fifteen seconds, three dial attempts each — for
#: as long as the slot stays unsatisfied. That is a phone bill and a modem
#: held busy for every other site, and it buys nothing: a meter that did not
#: answer a minute ago is answering no differently now. Long enough to matter,
#: short enough that a line which comes back is still polled within its hour.
RETRY_PAUSE = timedelta(minutes=15)

#: How many times one scheduled slot is attempted before it is left alone.
#:
#: Three calls, a quarter of an hour apart, cover what a retry can actually
#: fix: a busy line, a meter that was mid-something, a modem that had not come
#: back yet. After that the answer stops changing, and calling every fifteen
#: minutes until morning is a hundred pointless calls and a modem the rest of
#: the fleet cannot use. The site is not forgotten — its next scheduled hour
#: starts over.
MAX_ATTEMPTS_PER_SLOT = 3


def last_slot(now: datetime, poll_times: List[str]) -> Optional[datetime]:
    """The most recent scheduled moment that has already arrived.

    Today's slots up to `now`, and failing that yesterday's last one — a device
    scheduled only for 18:00 and looked at during the morning is not "not yet
    scheduled", it is due since yesterday evening.
    """
    parsed = []
    for value in poll_times or []:
        try:
            hour, minute = (int(part) for part in str(value).split(":"))
        except (ValueError, TypeError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed.append((hour, minute))
    if not parsed:
        return None

    today = [
        now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in parsed
    ]
    passed = [slot for slot in today if slot <= now]
    if passed:
        return max(passed)
    return max(today) - timedelta(days=1)


def is_due(
    *,
    now: datetime,
    poll_times: Optional[List[str]],
    default_times: List[str],
    last_poll_at: Optional[datetime],
    enabled: bool,
    auto_poll: bool,
    manual_requested_at: Optional[datetime] = None,
    last_attempt_at: Optional[datetime] = None,
    last_status: Optional[str] = None,
    scheduled_failures: Optional[List[datetime]] = None,
) -> Tuple[bool, str]:
    """Should this device be polled right now, and why (not).

    `poll_times` of None means the device follows the global hours; that is a
    default rather than the only option, so a device that needs its own rhythm
    carries it and everything else stays in one place.
    """
    if not enabled:
        return False, REASON_DISABLED

    # A manual request jumps the queue, and it outranks `auto_poll`: switching
    # the schedule off is a statement about the schedule, not a refusal to be
    # read when somebody asks.
    if manual_requested_at is not None and (
        last_poll_at is None or manual_requested_at > last_poll_at
    ):
        return True, REASON_MANUAL_REQUEST

    if not auto_poll:
        return False, REASON_MANUAL_ONLY

    # A failure is retried, but not immediately: see RETRY_PAUSE. Checked
    # after the manual branch on purpose — somebody who presses «Опитати»
    # having just watched it fail is allowed to try again at once.
    if (last_status and last_status != "ok" and last_attempt_at is not None
            and now - last_attempt_at < RETRY_PAUSE):
        return False, REASON_COOLING_OFF

    slot = last_slot(now, poll_times if poll_times is not None else default_times)
    if slot is None:
        # No hours anywhere: nothing to be late for. Not an error — a device
        # can be kept for manual polling only, with the schedule left empty.
        return False, REASON_NO_SLOTS

    # Tried enough for this slot. Counted per slot rather than in a row, so a
    # site that fails three times every morning is quiet until the next
    # scheduled hour instead of being dialled all day. Calls a person asked
    # for are not in this list: checking a site by hand must not spend the
    # automatic attempts it still had.
    failures = sum(1 for at in scheduled_failures or [] if at >= slot)
    if failures >= MAX_ATTEMPTS_PER_SLOT:
        return False, REASON_GAVE_UP

    if last_poll_at is None:
        return True, REASON_NEVER_POLLED
    if last_poll_at >= slot:
        return False, REASON_ALREADY_POLLED
    return True, REASON_OVERDUE
