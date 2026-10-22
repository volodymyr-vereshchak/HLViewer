"""Several records for one moment inside a single hostlib file.

A meter writes one record per hour (or per day), with two exceptions seen on
prod. When the clock goes back in October some meters write the repeated 03:00
as two records instead of one record holding both hours (27.10.2024, «Побут 1»
and «ВП ДЕЛЬТА»; every other meter writes a single 03:00 with double volume).
And after a power failure a meter may leave an empty record next to the real
one: volume 0, pressure and temperature holding the integer 1, which reads as
the float 1.4e-45 (06.12.2024, «ВП ДЕЛЬТА»).

The archive keeps one row per (line_id, period), so such a group is folded
into one record here, before it reaches the database:

* records with no volume are dropped while any record has volume — they are
  the empty ones and carry no gas;
* volumes are summed (both records are real gas that flowed through the
  meter), pressure, temperature and density are averaged.

When every record is empty, the ones whose pressure or temperature is the
"no data" marker are dropped instead, and the rest averaged the same way. If
nothing survives either filter the last record is kept as it is.

This is about one file only. The same moment coming from two files or two
exports is the same record read twice — that is never summed (see
`main.update_archive`).
"""
import logging

logger = logging.getLogger(__name__)

# Below this a volume is no gas at all: the meter's empty record holds 0 or
# the 1.4e-45 marker, real hours are whole cubic metres.
ZERO_VOLUME = 1e-6
# Pressure or temperature this close to zero is the "no data" marker, not a
# reading: the smallest real pressure on prod is ~0.9 and temperatures sit
# well away from exactly zero.
NO_DATA = 1e-3

SUMMED = ("volume", "w_volume_dp")
AVERAGED = ("pressure", "temperature", "density")


def _fold(group: list[dict]) -> dict:
    flowing = [r for r in group if abs(r["volume"]) > ZERO_VOLUME]
    measured = [
        r for r in group
        if abs(r["pressure"]) >= NO_DATA and abs(r["temperature"]) >= NO_DATA
    ]
    kept = flowing or measured or group[-1:]
    merged = dict(kept[-1])
    for field in SUMMED:
        if field in merged:
            merged[field] = sum(r[field] for r in kept)
    for field in AVERAGED:
        if field in merged:
            merged[field] = sum(r[field] for r in kept) / len(kept)
    return merged


def merge_same_period(records: list[dict], source: str = "") -> list[dict]:
    """One record per period, in the order the periods first appear."""
    groups: dict = {}
    for record in records:
        groups.setdefault(record["period"], []).append(record)
    if len(groups) == len(records):
        return records

    merged = []
    for period, group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        folded = _fold(group)
        logger.info(
            "%s: %d записи на %s склеєно в одну — об'єми %s → %s",
            source, len(group), period,
            [r["volume"] for r in group], folded["volume"],
        )
        merged.append(folded)
    return merged
