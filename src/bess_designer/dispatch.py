"""Optimal dispatch of an AC-coupled PV + BESS plant behind one grid connection.

The dispatch is a linear program solved with HiGHS (via ``scipy.optimize.linprog``).
It assumes perfect foresight of day-ahead prices for the whole year, so the
result is an upper bound of what a real trading strategy would earn. This is
standard for a first technical-economic screening.

Plant topology (AC-coupled)::

    PV array -> PV inverters (AC limit) --+
                                          +--> MV busbar --> POI / grid (export & import limit)
    Battery containers <-> PCS -----------+

Decision variables per hour t (all >= 0):

    pv_grid   PV energy sold to the grid               [MW]
    pv_bat    PV energy used to charge the battery      [MW]
    curt      PV curtailed                              [MW]
    grid_bat  grid energy used to charge the battery    [MW]
    dis       battery discharge to the grid             [MW]
    soc       battery energy content at end of hour t   [MWh]

Objective: maximise market revenue
    sum_t price_t * dt * (pv_grid + dis - grid_bat)
minus a grid charging fee and a small throughput cost that stands in for
battery wear (it stops the optimiser from cycling for tiny spreads).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import linprog

VARS = ("pv_grid", "pv_bat", "curt", "grid_bat", "dis", "soc")


@dataclass
class BessSpec:
    power_mw: float               # PCS rating (charge and discharge)
    usable_mwh: float             # energy between soc_min and soc_max
    nameplate_mwh: float          # installed energy
    soc_min_mwh: float
    soc_max_mwh: float
    soc_initial_mwh: float
    roundtrip_efficiency: float
    max_cycles_per_day: float | None = None


@dataclass
class GridSpec:
    export_limit_mw: float
    import_limit_mw: float
    allow_grid_charging: bool


@dataclass
class DispatchResult:
    timeseries: pd.DataFrame      # one column per variable plus price and pv
    revenue_eur: float            # market revenue of the whole plant
    objective_eur: float          # revenue minus fees and wear cost
    status: str


def pv_only_baseline(pv_mw: pd.Series, price: pd.Series, export_limit_mw: float, dt_h: float = 1.0) -> DispatchResult:
    """Reference case without storage. PV is sold up to the grid limit and
    switched off in hours with negative prices (no payment in those hours)."""
    sell = np.where(price.to_numpy() >= 0, np.minimum(pv_mw.to_numpy(), export_limit_mw), 0.0)
    curt = pv_mw.to_numpy() - sell
    zeros = np.zeros(len(pv_mw))
    ts = pd.DataFrame(
        {"pv": pv_mw.to_numpy(), "price": price.to_numpy(), "pv_grid": sell, "pv_bat": zeros,
         "curt": curt, "grid_bat": zeros, "dis": zeros, "soc": zeros},
        index=pv_mw.index,
    )
    rev = float((price.to_numpy() * sell).sum() * dt_h)
    return DispatchResult(ts, rev, rev, "analytic")


def optimise_dispatch(
    pv_mw: pd.Series,
    price: pd.Series,
    bess: BessSpec,
    grid: GridSpec,
    dt_h: float = 1.0,
    degradation_cost_eur_per_mwh: float = 0.0,
    grid_charging_fee_eur_per_mwh: float = 0.0,
) -> DispatchResult:
    """Solve the yearly dispatch LP and return hourly results."""
    T = len(pv_mw)
    pv = pv_mw.to_numpy(dtype=float)
    pr = price.to_numpy(dtype=float)
    eta = np.sqrt(bess.roundtrip_efficiency)  # split losses equally on charge/discharge
    n = len(VARS) * T

    def col(var: str, t: np.ndarray | int) -> np.ndarray | int:
        return VARS.index(var) * T + t

    t_all = np.arange(T)

    # ---- objective (linprog minimises)
    c = np.zeros(n)
    c[col("pv_grid", t_all)] = -pr * dt_h
    c[col("dis", t_all)] = -pr * dt_h + degradation_cost_eur_per_mwh * dt_h
    c[col("grid_bat", t_all)] = pr * dt_h + grid_charging_fee_eur_per_mwh * dt_h

    # ---- equality constraints
    rows, cols, vals, b_eq = [], [], [], []
    r = 0
    # (1) PV balance: pv_grid + pv_bat + curt = pv
    for v in ("pv_grid", "pv_bat", "curt"):
        rows.append(r + t_all); cols.append(col(v, t_all)); vals.append(np.ones(T))
    b_eq.append(pv)
    r += T
    # (2) SoC dynamics: soc_t - soc_{t-1} - eta*dt*(pv_bat+grid_bat) + dt/eta*dis = 0
    rows.append(r + t_all); cols.append(col("soc", t_all)); vals.append(np.ones(T))
    rows.append(r + t_all[1:]); cols.append(col("soc", t_all[:-1])); vals.append(-np.ones(T - 1))
    for v in ("pv_bat", "grid_bat"):
        rows.append(r + t_all); cols.append(col(v, t_all)); vals.append(np.full(T, -eta * dt_h))
    rows.append(r + t_all); cols.append(col("dis", t_all)); vals.append(np.full(T, dt_h / eta))
    rhs = np.zeros(T)
    rhs[0] = bess.soc_initial_mwh
    b_eq.append(rhs)
    r += T
    A_eq = sparse.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(r, n)
    )
    b_eq = np.concatenate(b_eq)

    # ---- inequality constraints
    rows, cols, vals, b_ub = [], [], [], []
    r = 0
    # (3) export limit at the POI: pv_grid + dis <= export_limit
    for v in ("pv_grid", "dis"):
        rows.append(r + t_all); cols.append(col(v, t_all)); vals.append(np.ones(T))
    b_ub.append(np.full(T, grid.export_limit_mw))
    r += T
    # (4) PCS rating shared by charging and discharging:
    #     pv_bat + grid_bat + dis <= P   (prevents full simultaneous charge and discharge)
    for v in ("pv_bat", "grid_bat", "dis"):
        rows.append(r + t_all); cols.append(col(v, t_all)); vals.append(np.ones(T))
    b_ub.append(np.full(T, bess.power_mw))
    r += T
    # (5) end of year SoC >= start SoC (no "free" energy from the initial charge)
    rows.append(np.array([r])); cols.append(np.array([col("soc", T - 1)])); vals.append(np.array([-1.0]))
    b_ub.append(np.array([-bess.soc_initial_mwh]))
    r += 1
    # (6) optional cycle limit: sum(dis)*dt <= cycles/day * usable * days
    if bess.max_cycles_per_day is not None and bess.usable_mwh > 0:
        rows.append(np.full(T, r)); cols.append(col("dis", t_all)); vals.append(np.full(T, dt_h))
        days = T * dt_h / 24.0
        b_ub.append(np.array([bess.max_cycles_per_day * bess.usable_mwh * days]))
        r += 1
    A_ub = sparse.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(r, n)
    )
    b_ub = np.concatenate(b_ub)

    # ---- variable bounds
    grid_charge_max = min(bess.power_mw, grid.import_limit_mw) if grid.allow_grid_charging else 0.0
    ub = {
        "pv_grid": grid.export_limit_mw,
        "pv_bat": bess.power_mw,
        "curt": None,
        "grid_bat": grid_charge_max,
        "dis": bess.power_mw,
        "soc": bess.soc_max_mwh,
    }
    lb = {"soc": bess.soc_min_mwh}
    bounds = []
    for v in VARS:
        bounds.extend([(lb.get(v, 0.0), ub[v])] * T)

    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(f"Dispatch LP failed: {res.message}")

    x = res.x.reshape(len(VARS), T)
    x = np.where(np.abs(x) < 1e-7, 0.0, x)
    ts = pd.DataFrame({v: x[i] for i, v in enumerate(VARS)}, index=pv_mw.index)
    ts.insert(0, "price", pr)
    ts.insert(0, "pv", pv)
    revenue = float((pr * (ts["pv_grid"] + ts["dis"] - ts["grid_bat"])).sum() * dt_h)
    return DispatchResult(ts, revenue, float(-res.fun), res.message)
