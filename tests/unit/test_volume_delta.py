"""Recomputing a volume on the reference gas composition.

The GERG-91 values below are not a second transcription: they were produced by
`hl_frontend/src/domain/flowRate/__oracle__/original.generated.mjs`, the module
`parity.test.ts` pins bit-for-bit against the original Ask2 calculation. So a
green run here means the Python port agrees with the DLL the meters use.
"""

import math

import pytest

from backend.services.gost30319 import compressibility_ratio, z_gerg91, z_std
from backend.services.volume_delta import GasState, volume_delta, volume_ratio

# (rho, N2 %, CO2 %, P MPa, t °C, Zc, Z) straight from the oracle.
ORACLE = [
    (0.6682, 0.0, 0.0, 0.5, -10, 0.9981065648744956, 0.9866796529955743),
    (0.7467, 1.9546, 0.613, 3.2357, 0.2565, 0.9977202624749196, 0.9072044039223117),
    (0.7424, 1.9546, 0.613, 9.6066, 17.6529, 0.9977505879462469, 0.8023702950531719),
    (0.82, 5.0, 2.0, 9.6066, 35, 0.997453586556, 0.8208584971684014),
]


class TestGost30319:
    @pytest.mark.parametrize("rho,n2,co2,p,t_c,zc,z", ORACLE)
    def test_matches_the_original_calculation(self, rho, n2, co2, p, t_c, zc, z):
        xa, xy = n2 / 100, co2 / 100
        assert z_std(rho, xa, xy) == pytest.approx(zc, rel=1e-14)
        assert z_gerg91(rho, xa, xy, p, t_c + 273.15) == pytest.approx(z, rel=1e-13)

    def test_impossible_state_is_none_not_an_exception(self):
        # A negative base under a cube root is NaN in JS and a complex number in
        # Python; the port must answer "cannot be evaluated" either way.
        assert compressibility_ratio(0.7467, 0.6, 2.0, 1e6, 300) is None

    def test_ratio_is_below_one_under_pressure(self):
        # Real gas at 3.2 MPa: Z well below 1, Zc essentially 1.
        k = compressibility_ratio(0.7467, 0.613, 1.9546, 3.2357, 273.4)
        assert 0.85 < k < 0.95


REF = GasState(density=0.7467, co2=0.613, n2=1.9546)
ENTERED = GasState(density=0.7424, co2=0.613, n2=1.9546)


class TestVolumeRatio:
    def test_identical_composition_changes_nothing(self):
        for is_meter in (True, False):
            assert volume_ratio(REF, REF, 3.2357, 0.2565, is_meter) == pytest.approx(1.0)

    def test_orifice_follows_the_square_root_of_density(self):
        # ρ and K sit under the same root; with the composition otherwise equal
        # the density term dominates and the ratio is ≈ √(ρ_in/ρ_ref).
        ratio = volume_ratio(ENTERED, REF, 3.2357, 0.2565, is_meter=False)
        assert ratio == pytest.approx(math.sqrt(0.7424 / 0.7467), rel=2e-3)
        assert ratio < 1  # entered density lower ⇒ reported volume overstated

    def test_meter_ignores_density_except_through_compressibility(self):
        ratio = volume_ratio(ENTERED, REF, 3.2357, 0.2565, is_meter=True)
        # Not 1 — K moved — but an order of magnitude closer to it than the
        # orifice, because the √ρ term is simply absent.
        orifice = volume_ratio(ENTERED, REF, 3.2357, 0.2565, is_meter=False)
        assert abs(ratio - 1) < abs(orifice - 1)

    def test_co2_and_n2_move_a_meter(self):
        other = GasState(density=0.7467, co2=2.5, n2=4.0)
        ratio = volume_ratio(other, REF, 3.2357, 0.2565, is_meter=True)
        assert ratio != pytest.approx(1.0, abs=1e-6)

    def test_zero_density_cannot_be_evaluated(self):
        assert volume_ratio(GasState(0.0, 0.6, 2.0), REF, 3.2, 0, False) is None


class TestVolumeDelta:
    def test_sign_is_reference_minus_reported(self):
        delta = volume_delta(1000.0, ENTERED, REF, 3.2357, 0.2565, is_meter=False)
        # Entered density below the reference ⇒ the reported volume was too big
        # ⇒ the correction is negative.
        assert delta < 0
        assert delta / 1000.0 == pytest.approx(-0.002151, abs=1e-6)

    def test_no_flow_no_error(self):
        # The hour carried no gas, so however wrong its composition was it
        # cannot have mismeasured anything.
        assert volume_delta(0.0, ENTERED, REF, 3.2357, 0.2565, False) == 0.0

    def test_compressibility_damps_the_density_effect(self):
        """The K term is NOT a rounding detail — it removes a quarter of it.

        A lighter gas has a Z closer to 1, so K rises just as √ρ falls, and the
        two partly cancel. At 3.24 MPa the naive ½·Δρ/ρ estimate overstates the
        correction by about 25 %, which is why the report carries the full
        formula rather than the rule of thumb.
        """
        naive = 0.5 * (0.7424 - 0.7467) / 0.7467
        actual = volume_delta(1000.0, ENTERED, REF, 3.2357, 0.2565, False) / 1000.0
        assert naive == pytest.approx(-0.002879, abs=1e-6)
        assert abs(actual) < abs(naive)
        assert abs(actual / naive) == pytest.approx(0.747, abs=0.01)

    @pytest.mark.parametrize(
        "p_mpa,expected_pct",
        [(0.5, -0.2802), (2.0, -0.2472), (3.2357, -0.2151), (6.0, -0.1261)],
    )
    def test_the_effect_shrinks_with_pressure(self, p_mpa, expected_pct):
        """Same density error, four pressures: the higher the line runs, the
        less a wrong density costs. Pinned because it is counter-intuitive and
        a regression here would be invisible in any single number."""
        delta = volume_delta(1000.0, ENTERED, REF, p_mpa, 0.2565, is_meter=False)
        assert delta / 10.0 == pytest.approx(expected_pct, abs=1e-4)

    def test_outside_the_equations_domain_is_none(self):
        # GERG-91 мод. gives up above ~12 MPa — the ORIGINAL calculation does
        # too (checked against the oracle), so an unreadable hour is inherited
        # behaviour, not something this port introduced.
        assert volume_delta(1000.0, ENTERED, REF, 12.0, 0.2565, False) is None
