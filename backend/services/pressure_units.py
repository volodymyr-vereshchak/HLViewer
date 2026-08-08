"""Pressure units, mirroring `hl_frontend/src/domain/pressureUnits.ts`.

The archive stores pressure in the unit CONFIGURED ON THE LINE
(`gas_volume_line.pressure_unit`, кгс/см² by default) — not in a canonical one.
Nothing in the archive marks it, so a reader that assumes MPa is silently out
by a factor of ten: 41.86 кгс/см² is 4.1 MPa, and taken as 41.86 MPa it is past
anything ГОСТ 30319.2 can evaluate.

Order and factors match `P_UNITS` in the TS file, which in turn matches
Units.ListUnits from CalcDSTU8586.dll.
"""

# Unit label → pascals per unit.
PA_PER_UNIT: dict[str, float] = {
    "Па": 1.0,
    "кПа": 1e3,
    "МПа": 1e6,
    "бар": 1e5,
    "кгс/см²": 98066.5,
    "кгс/м²": 9.80665,
    "PSI": 6894.76,
    "мм рт.ст": 133.322,
}

PRESSURE_UNIT_DEFAULT = "кгс/см²"

# Values that mean "no unit recorded" rather than a unit — the same set the
# frontend guards against in `pressureUnits.ts`. A line left like this is
# reporting in the archive default, not in something unknown.
_ABSENT = {"", "none", "null", "nan", "n/a", "-", "—", "--"}


def to_mpa(value: float, unit: str | None) -> float | None:
    """Absolute pressure in MPa, or None when the unit is not one we know.

    The archive stores what the device measured in the line's own unit, and
    `lineAutofill.ts:128` records that it is ABSOLUTE pressure — nothing
    barometric has to be added.

    None rather than a guess for a genuinely unrecognised unit: feeding an
    unconverted number into the equation of state produces a plausible-looking
    Z for a pressure the line never saw, which is worse than a gap.
    """
    label = (unit or "").strip()
    if label.lower() in _ABSENT:
        label = PRESSURE_UNIT_DEFAULT
    factor = PA_PER_UNIT.get(label)
    if factor is None:
        return None
    return value * factor / 1e6
