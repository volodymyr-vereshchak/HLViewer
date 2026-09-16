"""Validating what an operator types on a poll card.

Split out of the endpoint because each of these is a rule with a reason, and
each reason is a way a poll fails on somebody else's machine hours later:

  * a phone number the modem cannot dial produces "no dialtone", which reads
    exactly like a dead line;
  * a poll hour that is not an hour is a slot the agent silently never reaches;
  * a network address that is not the one the device answers on produces
    "помилка адреси", which reads exactly like a dead meter.

None of them can be caught by the person who typed them, so they are caught
here.
"""
import re
from typing import List, Optional

# Ukrainian numbers only, and always in one shape: +380 and nine digits. The
# fleet is domestic, the agent hands this straight to ATDP, and a modem does
# not guess — "050…" and "+38(050)…" reach nothing.
PHONE_RE = re.compile(r"^\+380\d{9}$")

# Priority 0…5, 0 first. A range rather than a free integer because it is a
# queue order an operator compares by eye, and "priority 900" tells nobody
# anything about where it sits.
PRIORITY_MIN = 0
PRIORITY_MAX = 5

# Protocols where the network address is a real choice: several Floutek
# correctors share one line and answer on their own addresses. Every other
# driver sends the address too and checks it in the reply, but with one device
# per line it stays at its default — see poll_device.device_address.
FLOUTEK_PROTOCOLS = frozenset({
    999,   # Флоутек ВР-1 (РД)
    1000,  # Флоутек ВР-1 (в.37)
    1062,  # Флоутек-ТМ-3-4, v.40
    1070,  # Флоутек ВР-2
    1071,  # Флоутек ВР-2 (E_kWh)
    2002,  # Флоутек ТМ-2 — our own reader, not an Ask2 driver
})


DEFAULT_DEVICE_ADDRESS = 1


class PollValidationError(ValueError):
    """A message meant for the operator, not a traceback."""


def normalise_phone(raw: Optional[str]) -> Optional[str]:
    """Bring a typed number to +380XXXXXXXXX, or refuse it.

    Punctuation and spaces are dropped and the common local shapes are
    accepted, because that is how numbers are written down: 050…, 380…, 8050…
    all mean the same line. What is NOT accepted is a number that cannot be
    dialled — better a red field now than "no dialtone" in a log tomorrow.
    """
    if raw is None:
        return None
    digits = re.sub(r"[\s\-()]", "", raw.strip())
    if not digits:
        return None

    if digits.startswith("+"):
        digits = "+" + re.sub(r"\D", "", digits[1:])
    else:
        digits = re.sub(r"\D", "", digits)
        if digits.startswith("0") and len(digits) == 10:
            digits = "+38" + digits          # 0501234567
        elif digits.startswith("80") and len(digits) == 11:
            digits = "+3" + digits           # 80501234567 (old inter-city)
        elif digits.startswith("380"):
            digits = "+" + digits            # 380501234567
        else:
            digits = "+" + digits

    if not PHONE_RE.match(digits):
        raise PollValidationError(
            f"Некоректний номер: {raw!r}. Очікується український номер "
            f"у вигляді +380XXXXXXXXX"
        )
    return digits


def validate_poll_times(poll_times: Optional[List[str]]) -> Optional[List[str]]:
    """Normalise "HH:MM" slots, sorted and without duplicates.

    Sorted because the list is read as a daily rhythm and "18:00, 06:00" makes
    that harder than it needs to be; de-duplicated because the same slot twice
    is one slot, and the agent would otherwise count it twice.
    """
    if poll_times is None:
        return None
    out = set()
    for value in poll_times:
        try:
            hour_s, minute_s = str(value).strip().split(":")
            hour, minute = int(hour_s), int(minute_s)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            raise PollValidationError(
                f"Некоректний час опитування: {value!r}. Очікується HH:MM"
            )
        out.add(f"{hour:02d}:{minute:02d}")
    return sorted(out)


def validate_priority(priority: Optional[int]) -> Optional[int]:
    if priority is None:
        return None
    if not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
        raise PollValidationError(
            f"Пріоритет має бути від {PRIORITY_MIN} (найвищий) "
            f"до {PRIORITY_MAX}"
        )
    return priority


def address_matters(protocol_id: Optional[int]) -> bool:
    """Is the network address a real choice for this driver?

    Only for Floutek, where several correctors share a line. Everywhere else
    the address is sent and checked but stays at its default, so asking an
    operator for it is asking for a typo that looks like a dead meter.
    """
    return protocol_id in FLOUTEK_PROTOCOLS
