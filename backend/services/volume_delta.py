"""What the volume would have been on the reference gas composition.

Derived from the project's own flow model (`hl_frontend/src/domain/flowRate/
calculate.ts`), where for a metering line

    лічильник :  qStd = qW · (P/P0)(T0/T) / K            (calculate.ts:202)
    діафрагма :  qStd = qm / ρ,  qm ∝ √(Δp·ρw),  ρw = ρ·Kp,
                 Kp = (P/P0)(T0/T)/K                     (calculate.ts:150,198)

Cancelling everything that is the same for both compositions — geometry, Δp,
P, T — leaves

                ⎛ K_in ⎞^a   ⎛ ρ_in ⎞^b
    V_ref = V · ⎜ ──── ⎟   · ⎜ ──── ⎟
                ⎝ K_ref⎠     ⎝ ρ_ref⎠

    лічильник : a = 1,  b = 0   — density does not enter a volume meter's
                                  conversion at all; the whole effect of the
                                  composition is the compressibility ratio.
    діафрагма : a = ½,  b = ½   — ρ and K sit under the same square root,
                                  because mass flow goes as √(Δp·ρw).

K is GERG-91 (ГОСТ 30319.2) over Zc, by decision — see gost30319.py.

NOT included for the orifice: the discharge coefficient C (through Reynolds and
viscosity) and the expansibility ε (through the adiabat) also move with the
composition, but C is solved iteratively and does not fold into a closed form.
Their contribution is hundredths of a percent against the ~0.3 % the density
error itself produces. If that ever stops being good enough, the honest route
is running the full model twice per hour rather than adding terms here.
"""

from dataclasses import dataclass
from typing import Optional

from backend.services.gost30319 import compressibility_ratio


@dataclass(frozen=True)
class GasState:
    """One composition as it applies to one hour."""

    density: float
    co2: float
    n2: float


def volume_ratio(
    entered: GasState,
    reference: GasState,
    p_mpa: float,
    t_c: float,
    is_meter: bool,
) -> Optional[float]:
    """V_ref / V_entered, or None when the state cannot be evaluated.

    Pressure and temperature are MEASURED, so they are the same for both
    compositions and only decide where on the K surface each one is read.
    """
    if entered.density <= 0 or reference.density <= 0:
        return None

    t_k = t_c + 273.15
    k_in = compressibility_ratio(entered.density, entered.co2, entered.n2, p_mpa, t_k)
    k_ref = compressibility_ratio(
        reference.density, reference.co2, reference.n2, p_mpa, t_k
    )
    if not k_in or not k_ref:
        return None

    if is_meter:
        return k_in / k_ref
    return ((k_in / k_ref) * (entered.density / reference.density)) ** 0.5


def volume_delta(
    volume: float,
    entered: GasState,
    reference: GasState,
    p_mpa: float,
    t_c: float,
    is_meter: bool,
) -> Optional[float]:
    """ΔV = V_reference − V_entered for ONE hour.

    Negative means the reported volume was overstated. Always per hour: the
    ratio is non-linear and the flow is not spread evenly over a day, so an
    hour that carried no gas contributes nothing however wrong its composition
    was. Daily figures are the SUM of these, never a recomputation from daily
    averages.
    """
    ratio = volume_ratio(entered, reference, p_mpa, t_c, is_meter)
    if ratio is None:
        return None
    return volume * (ratio - 1.0)
