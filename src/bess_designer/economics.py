"""Simple project economics for a BESS added to an existing or planned PV plant.

All figures are *incremental*: the value of the battery is the plant revenue
with BESS minus the revenue of the same PV plant without BESS.

Simplifications (stated in every report):
* Year-1 dispatch is simulated with one historical year of prices and weather.
* Later years scale the year-1 margin by the remaining usable capacity
  (linear capacity fade). No augmentation, no price escalation, no tax.
* LCOS excludes the cost of the charging energy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq


@dataclass
class Cashflows:
    capex_eur: float
    years: np.ndarray            # 1..N
    revenue_eur: np.ndarray      # incremental market revenue per year
    opex_eur: np.ndarray
    net_eur: np.ndarray
    discharged_mwh: np.ndarray


@dataclass
class EconomicKpis:
    capex_eur: float
    capex_eur_per_kwh_usable: float
    year1_margin_eur: float
    npv_eur: float
    irr: float | None
    simple_payback_years: float | None
    lcos_eur_per_mwh: float


def capex(nameplate_mwh: float, power_mw: float, eur_per_kwh: float, eur_per_kw: float) -> float:
    return nameplate_mwh * 1000 * eur_per_kwh + power_mw * 1000 * eur_per_kw


def build_cashflows(
    capex_eur: float,
    year1_revenue_eur: float,
    year1_discharged_mwh: float,
    life_years: int,
    annual_fade: float,
    opex_pct_of_capex: float,
) -> Cashflows:
    years = np.arange(1, life_years + 1)
    capacity_factor = np.clip(1.0 - annual_fade * (years - 1), 0.0, 1.0)
    revenue = year1_revenue_eur * capacity_factor
    discharged = year1_discharged_mwh * capacity_factor
    opex = np.full(life_years, capex_eur * opex_pct_of_capex)
    return Cashflows(capex_eur, years, revenue, opex, revenue - opex, discharged)


def npv(rate: float, capex_eur: float, net: np.ndarray) -> float:
    years = np.arange(1, len(net) + 1)
    return float(-capex_eur + (net / (1 + rate) ** years).sum())


def irr(capex_eur: float, net: np.ndarray) -> float | None:
    f = lambda r: npv(r, capex_eur, net)
    if f(-0.99) * f(1.0) > 0:
        # no sign change between -99 % and +100 %
        return None
    try:
        return float(brentq(f, -0.99, 1.0, xtol=1e-6))
    except ValueError:
        return None


def simple_payback(capex_eur: float, net: np.ndarray) -> float | None:
    cum = np.cumsum(net)
    if cum[-1] < capex_eur:
        return None
    i = int(np.argmax(cum >= capex_eur))
    before = cum[i - 1] if i > 0 else 0.0
    return float(i + (capex_eur - before) / net[i])


def lcos(rate: float, cf: Cashflows) -> float:
    disc = (1 + rate) ** cf.years
    energy = (cf.discharged_mwh / disc).sum()
    if energy <= 0:
        return float("inf")
    return float((cf.capex_eur + (cf.opex_eur / disc).sum()) / energy)


def evaluate(cf: Cashflows, rate: float, usable_mwh: float) -> EconomicKpis:
    return EconomicKpis(
        capex_eur=cf.capex_eur,
        capex_eur_per_kwh_usable=cf.capex_eur / (usable_mwh * 1000) if usable_mwh else float("nan"),
        year1_margin_eur=float(cf.net_eur[0]),
        npv_eur=npv(rate, cf.capex_eur, cf.net_eur),
        irr=irr(cf.capex_eur, cf.net_eur),
        simple_payback_years=simple_payback(cf.capex_eur, cf.net_eur),
        lcos_eur_per_mwh=lcos(rate, cf),
    )
