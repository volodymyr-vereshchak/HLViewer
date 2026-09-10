"""What a poll card refuses to store.

Each rule here is a failure that would otherwise surface on an operator's
machine hours later and look like broken equipment: an undialable number reads
as "no dialtone", a slot that is not an hour is simply never reached, and a
wrong network address reads as "помилка адреси". None of them can be spotted by
the person who typed them.
"""

import pytest

from backend.services.poll_validation import (
    PollValidationError,
    address_matters,
    normalise_phone,
    validate_poll_times,
    validate_priority,
)


class TestPhone:
    """One shape, +380 and nine digits: the agent hands it straight to ATDP."""

    @pytest.mark.parametrize("raw", [
        "+380501234567",
        "380501234567",
        "0501234567",
        "80501234567",
        " +38 (050) 123-45-67 ",
    ])
    def test_the_shapes_people_write_all_normalise(self, raw):
        # These are all the same line, written the way it happened to be
        # written down; refusing them would be pedantry.
        assert normalise_phone(raw) == "+380501234567"

    @pytest.mark.parametrize("raw", [
        "050123456",        # a digit short
        "05012345678",      # a digit too many
        "+7501234567",      # not Ukrainian
        "+380abc123456",
        "телефон у бухгалтерії",
    ])
    def test_what_cannot_be_dialled_is_refused(self, raw):
        with pytest.raises(PollValidationError):
            normalise_phone(raw)

    def test_no_number_is_a_valid_answer(self):
        # A card can exist before somebody finds out the number.
        assert normalise_phone(None) is None
        assert normalise_phone("   ") is None


class TestPollTimes:
    def test_slots_come_back_padded_and_sorted(self):
        # The list is read as a daily rhythm, so "18:00, 06:00" makes that
        # harder than it needs to be.
        assert validate_poll_times(["18:00", "6:5"]) == ["06:05", "18:00"]

    def test_the_same_slot_twice_is_one_slot(self):
        assert validate_poll_times(["06:00", "06:00"]) == ["06:00"]

    @pytest.mark.parametrize("value", ["25:00", "06:60", "6", "ранок", "06:00:00"])
    def test_what_is_not_an_hour_is_refused(self, value):
        # A slot the agent silently never reaches is worse than an error.
        with pytest.raises(PollValidationError):
            validate_poll_times([value])

    def test_none_means_use_the_global_hours(self):
        assert validate_poll_times(None) is None

    def test_an_empty_list_stays_empty(self):
        # Not the same as None: it says "no hours of its own", which the caller
        # is free to treat as it likes.
        assert validate_poll_times([]) == []


class TestPriority:
    @pytest.mark.parametrize("value", [0, 3, 5])
    def test_the_allowed_range(self, value):
        assert validate_priority(value) == value

    @pytest.mark.parametrize("value", [-1, 6, 900])
    def test_outside_it_is_refused(self, value):
        # A queue order is compared by eye; "priority 900" says nothing about
        # where it sits.
        with pytest.raises(PollValidationError):
            validate_priority(value)

    def test_omitted_is_left_alone(self):
        assert validate_priority(None) is None


class TestAddressMatters:
    @pytest.mark.parametrize("protocol", [999, 1000, 1062, 1070, 1071])
    def test_floutek_shares_a_line_so_the_address_is_a_choice(self, protocol):
        assert address_matters(protocol) is True

    @pytest.mark.parametrize("protocol", [1052, 1054, 71, 72, 1077, None])
    def test_everywhere_else_it_stays_at_its_default(self, protocol):
        # The address is still sent and still checked in the reply — it is just
        # never anything but 1, so asking for it invites a typo.
        assert address_matters(protocol) is False
