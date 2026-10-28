"""Normalisation of unit strings coming from the DPD API."""
from backend.services.pressure_units import canonical

# Values that mean "the device reported no unit". Part of the correctors answer
# with the literal string "None"/"null" instead of omitting pressUnit; stored as
# it is, it reaches the UI as «Тиск, None» instead of falling back to the default.
_ABSENT = {"", "none", "null", "nan", "n/a", "-", "—", "--"}


def volume_not_negative(value):
    """A period volume, with anything below zero stored as 0.

    A negative day or hour is a totaliser that was set back — a corrector
    replaced or its archive cleared — measured across the jump: "new reading
    minus old" is minus everything the old one ever counted. No gas flowed
    backwards; summed into a line it showed −1.7 million m³ and broke the
    whole enterprise report. None (no data) stays None.
    """
    if value is not None and value < 0:
        return 0.0
    return value


def normalize_press_unit(raw) -> str | None:
    """Pressure unit as reported by DPD, or None when the API said nothing.

    Under the name the rest of the system knows it by: the API says кгс/см3
    for кгс/см², and a row labelled with a unit nothing can convert is a row
    nobody can read.
    """
    if raw is None:
        return None
    text = raw.strip() if isinstance(raw, str) else str(raw).strip()
    if text.lower() in _ABSENT:
        return None
    return canonical(text) or text
