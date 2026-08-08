"""Decoding `edit_archive.old_value` / `new_value`.

The device writes a float32 and the archive stores its 32 bits as a signed
int32, so reading the number back is a reinterpretation, not a conversion:
`struct.unpack("!f", struct.pack("!i", raw))`.

Two deliberate differences from the frontend's `formatEditValue`
(`hl_frontend/src/domain/valueConverter.ts:105-182`), which decodes the same
column for the «Архів змін» table:

  * The frontend treats `|raw| <= 32767` as a plain integer, because for enum
    and flag edit types that is what the device means. For ФХП we never do:
    such a bit pattern is a denormal ≈ 1e-40, i.e. zero or garbage, and no
    density/CO2/N2 value in the archive falls in that band (checked over
    110290 rows — zero hits on new_value).
  * The frontend's firmware-specific decoders (DST rule, INT16, TEXT, HEX) are
    gated to verified computer types and never apply to codes 1/2/3.

If those two ever disagree, the same archive row would read differently in the
change archive and in the ФХП report — so keep this comment and its twin in
valueConverter.ts in sync.
"""

import math
import struct

# edit_type_id → (min, max) a real reading can take. Values outside are not
# applied: one 1e30 would otherwise poison its hour, that hour's day, the
# reference built from it, and every line's deviation against that reference.
PLAUSIBLE: dict[int, tuple[float, float]] = {
    1: (0.40, 1.50),   # густина, кг/м³
    2: (0.00, 10.00),  # CO2, мол.%
    3: (0.00, 20.00),  # N2, мол.%
}


def decode_float(raw: int) -> float | None:
    """int32 bit pattern → float32. None for NaN/Inf."""
    if raw is None:
        return None
    try:
        value = struct.unpack("!f", struct.pack("!i", int(raw)))[0]
    except (struct.error, OverflowError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def encode_float(value: float) -> int:
    """Inverse of `decode_float` — used by tests and any seeding script."""
    return struct.unpack("!i", struct.pack("!f", value))[0]


def is_plausible(edit_type_id: int, value: float | None) -> bool:
    if value is None:
        return False
    band = PLAUSIBLE.get(edit_type_id)
    if band is None:
        return True
    low, high = band
    return low <= value <= high
