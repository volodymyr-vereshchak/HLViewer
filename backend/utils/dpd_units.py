"""Normalisation of unit strings coming from the DPD API."""

# Values that mean "the device reported no unit". Part of the correctors answer
# with the literal string "None"/"null" instead of omitting pressUnit; stored as
# it is, it reaches the UI as «Тиск, None» instead of falling back to the default.
_ABSENT = {"", "none", "null", "nan", "n/a", "-", "—", "--"}


def normalize_press_unit(raw) -> str | None:
    """Pressure unit as reported by DPD, or None when the API said nothing."""
    if raw is None:
        return None
    text = raw.strip() if isinstance(raw, str) else str(raw).strip()
    return None if text.lower() in _ABSENT else text
