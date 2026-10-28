"""Normalisation of values coming from the DPD API and GSM polls."""
import pytest

from backend.utils.dpd_units import volume_not_negative


@pytest.mark.parametrize("raw, stored", [
    (-1771792.28, 0.0),   # a totaliser set back, measured across the jump
    (-0.001, 0.0),
    (0.0, 0.0),
    (12.5, 12.5),
    (None, None),         # no data stays no data, not a zero
])
def test_volume_not_negative(raw, stored):
    assert volume_not_negative(raw) == stored
