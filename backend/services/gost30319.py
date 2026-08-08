"""ГОСТ 30319 — the pieces «Звірка ФХП» needs to recompute a volume.

A 1:1 transcription of `hl_frontend/src/domain/flowRate/gost30319.ts`, which is
itself a 1:1 port of GOST30319.dll as used by Ask2 and is pinned against the
DLL by `src/domain/flowRate/parity.test.ts`. Do not "simplify" the expressions:
they are the reference, not an approximation of it.

Only `z_std` and `z_gerg91` live here. The volume recalculation needs the
compressibility ratio K = Z/Zc and nothing else; the discharge coefficient,
viscosity and adiabat stay on the frontend with the rest of the flow model.

Argument convention, as in the TS:
    rho — standard density, kg/m³
    xa  — N₂ mole FRACTION (0…1, i.e. percent / 100)
    xy  — CO₂ mole fraction
    p   — absolute pressure, MPa
    t   — temperature, K
"""

import math

NAN = float("nan")


def _sqrt(x: float) -> float:
    """JS `Math.sqrt`: NaN for a negative argument, not an exception."""
    return math.sqrt(x) if x >= 0 else NAN


def _cbrt(x: float) -> float:
    """JS `Math.pow(x, 1/3)`: NaN for a negative base, not a complex root.

    Python would happily return a complex number here and the arithmetic would
    carry on producing nonsense. Matching JS keeps the port faithful and lets
    an impossible state surface as NaN, which the caller already checks for.
    """
    return x ** (1 / 3) if x >= 0 else NAN


def z_std(rho: float, xa: float, xy: float) -> float:
    """ф. 36 — compressibility factor at standard conditions."""
    return 1 - (0.0741 * rho - 0.006 - 0.063 * xa - 0.0575 * xy) ** 2


def z_gerg91(rho: float, xa: float, xy: float, p: float, t: float) -> float:
    """GERG-91 мод. — ГОСТ 30319.2, ф. 20…22, 34, 35, 37, 43."""
    xe = 1 - xa - xy  # ф. 22
    me = (24.05525 * z_std(rho, xa, xy) * rho - 28.0135 * xa - 44.01 * xy) / xe  # ф. 35
    h = 128.64 + 47.479 * me  # ф. 34

    # ф. 20 — second virial coefficient Bm
    f0 = 0.72 + 1.875e-5 * (320 - t) ** 2
    b22 = -0.86834 + 0.0040376 * t - 5.1657e-6 * t**2
    b12 = -0.339693 + 0.00161176 * t - 2.04429e-6 * t**2
    b11 = -0.1446 + 0.00074091 * t - 9.1195e-7 * t**2
    bee = (
        -0.425468
        + 0.002865 * t
        - 4.62073e-6 * t**2
        + (8.77118e-4 - 5.56281e-6 * t + 8.81514e-9 * t**2) * h
        + (-8.24747e-7 + 4.31436e-9 * t - 6.08319e-12 * t**2) * h * h
    )
    bm = (
        xe**2 * bee
        + xe * xa * f0 * (bee + b11)
        - 1.73 * xe * xy * _sqrt(bee * b22)
        + xa**2 * b11
        + 2 * xa * xy * b12
        + xy**2 * b22
    )

    # ф. 21 — third virial coefficient Cm
    g0 = 0.92 + 0.0013 * (t - 270)
    c122 = 0.00358783 + 8.06674e-6 * t - 3.25798e-8 * t**2
    c112 = 0.00552066 - 1.68609e-5 * t + 1.57169e-8 * t**2
    c222 = 0.0020513 + 3.4888e-5 * t - 8.3703e-8 * t**2
    c111 = 0.0078498 - 3.9895e-5 * t + 6.1187e-8 * t**2
    ceee = (
        -0.302488
        + 0.00195861 * t
        - 3.16302e-6 * t**2
        + (6.46422e-4 - 4.22876e-6 * t + 6.88157e-9 * t**2) * h
        + (-3.32805e-7 + 2.2316e-9 * t - 3.67713e-12 * t**2) * h**2
    )
    cm = (
        xe**3 * ceee
        + 3 * xe**2 * xa * g0 * _cbrt(ceee * ceee * c111)
        + 2.76 * xe**2 * xy * _cbrt(ceee * ceee * c222)
        + 3 * xe * xa * xa * g0 * _cbrt(ceee * c111 * c111)
        + 6.6 * xe * xa * xy * _cbrt(ceee * c111 * c222)
        + 2.76 * xe * xy**2 * _cbrt(ceee * c222 * c222)
        + xa**3 * c111
        + 3 * xa * xa * xy * c112
        + 3 * xa * xy * xy * c122
        + xy**3 * c222
    )

    b = (1000 * p) / (2.7715 * t)  # ф. 43
    d1 = 1 + b * bm
    d2 = 1 + 1.5 * (b * bm + b * b * cm)
    d3 = _cbrt(d2 - _sqrt(d2 * d2 - d1**3))
    return (1 + d3 + d1 / d3) / 3  # ф. 37


def compressibility_ratio(
    rho: float, co2_pct: float, n2_pct: float, p_mpa: float, t_k: float
) -> float | None:
    """K = Z/Zc — what ГОСТ 30319.1 ф. 6 calls the compressibility ratio.

    Takes the composition in PERCENT, the way the archive stores it. Returns
    None when the arguments put the equations outside their domain (a negative
    root, a zero denominator) rather than raising: one impossible hour must not
    kill a month's report.
    """
    try:
        xa = n2_pct / 100.0
        xy = co2_pct / 100.0
        zc = z_std(rho, xa, xy)
        if zc == 0:
            return None
        z = z_gerg91(rho, xa, xy, p_mpa, t_k)
        if z <= 0 or not math.isfinite(z):
            return None
        return z / zc
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
