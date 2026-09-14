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

# Values that mean "no unit recorded" rather than a unit — the same set the
# frontend guards against in `pressureUnits.ts`. A line left like this is
# reporting in the archive default, not in something unknown.
_ABSENT = {"", "none", "null", "nan", "n/a", "-", "—", "--"}

PRESSURE_UNIT_DEFAULT = "кгс/см²"

# The same unit, written the several ways the archive holds it. `кгс/см3` is
# not a unit at all — it is what the DPD API calls кгс/см², on 157k rows — and
# taken literally it is unknown, which here means the reading is dropped
# rather than converted. Mirrors UNIT_ALIASES in pressureUnits.ts.
ALIASES: dict[str, str] = {
    "кгс/см3": "кгс/см²",
    "кгс/см2": "кгс/см²",
    "кг/см2": "кгс/см²",
    "кг/см3": "кгс/см²",
    "kgf/cm2": "кгс/см²",
    "kgf/cm3": "кгс/см²",
    "kg/cm2": "кгс/см²",
    "кгс/м2": "кгс/м²",
    "kgf/m2": "кгс/м²",
    "mpa": "МПа",
    "kpa": "кПа",
    "pa": "Па",
    "bar": "бар",
    "psi": "PSI",
    "мм рт.ст.": "мм рт.ст",
    "мм рт. ст.": "мм рт.ст",
    "mm hg": "мм рт.ст",
}


def canonical(unit: str | None) -> str | None:
    """The unit under the name the tables know it by, or None for nothing."""
    label = (unit or "").strip()
    if label.lower() in _ABSENT:
        return None
    return ALIASES.get(label.lower(), label)



def to_mpa(value: float, unit: str | None) -> float | None:
    """Absolute pressure in MPa, or None when the unit is not one we know.

    The archive stores what the device measured in the line's own unit, and
    `lineAutofill.ts:128` records that it is ABSOLUTE pressure — nothing
    barometric has to be added.

    None rather than a guess for a genuinely unrecognised unit: feeding an
    unconverted number into the equation of state produces a plausible-looking
    Z for a pressure the line never saw, which is worse than a gap.
    """
    label = canonical(unit) or PRESSURE_UNIT_DEFAULT
    factor = PA_PER_UNIT.get(label)
    if factor is None:
        return None
    return value * factor / 1e6
