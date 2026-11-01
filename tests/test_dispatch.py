"""Physics and logic checks for the dispatch LP."""

import numpy as np
import pandas as pd
import pytest

from bess_designer.dispatch import BessSpec, GridSpec, optimise_dispatch, pv_only_baseline


def _series(values, name):
    idx = pd.date_range("2023-06-01", periods=len(values), freq="h", tz="UTC")
    return pd.Series(np.asarray(values, dtype=float), index=idx, name=name)


def _bess(power=10.0, nameplate=20.0, rte=0.9, soc_init=0.5, cycles=None):
    return BessSpec(
        power_mw=power, usable_mwh=nameplate * 0.9, nameplate_mwh=nameplate,
        soc_min_mwh=0.05 * nameplate, soc_max_mwh=0.95 * nameplate,
        soc_initial_mwh=soc_init * nameplate, roundtrip_efficiency=rte, max_cycles_per_day=cycles,
    )


@pytest.fixture
def summer_week():
    rng = np.random.default_rng(1)
    h = np.arange(24 * 7)
    pv = np.clip(np.sin((h % 24 - 6) / 12 * np.pi), 0, None) * 40.0
    price = 80 + 40 * np.cos((h % 24 - 19) / 24 * 2 * np.pi) - 1.6 * pv + rng.normal(0, 5, len(h))
    return _series(pv, "pv"), _series(price, "price")


def test_constraints_hold(summer_week):
    pv, price = summer_week
    bess = _bess()
    grid = GridSpec(export_limit_mw=30.0, import_limit_mw=30.0, allow_grid_charging=True)
    ts = optimise_dispatch(pv, price, bess, grid).timeseries
    tol = 1e-6
    # PV energy balance
    assert np.allclose(ts.pv_grid + ts.pv_bat + ts.curt, ts.pv, atol=1e-6)
    # grid connection limit
    assert (ts.pv_grid + ts.dis <= grid.export_limit_mw + tol).all()
    # PCS rating
    assert (ts.pv_bat + ts.grid_bat + ts.dis <= bess.power_mw + tol).all()
    # state of charge window
    assert (ts.soc >= bess.soc_min_mwh - tol).all() and (ts.soc <= bess.soc_max_mwh + tol).all()
    # nothing negative
    assert (ts[["pv_grid", "pv_bat", "curt", "grid_bat", "dis"]] >= -tol).all().all()


def test_energy_balance_with_losses(summer_week):
    pv, price = summer_week
    bess = _bess(rte=0.81)  # eta = 0.9 each way
    ts = optimise_dispatch(pv, price, bess, GridSpec(30, 30, True)).timeseries
    eta = 0.9
    soc_prev = np.concatenate([[bess.soc_initial_mwh], ts.soc.to_numpy()[:-1]])
    expected = soc_prev + eta * (ts.pv_bat + ts.grid_bat) - ts.dis / eta
    assert np.allclose(ts.soc, expected, atol=1e-6)
    # no free energy: end at least where we started
    assert ts.soc.iloc[-1] >= bess.soc_initial_mwh - 1e-6


def test_buy_low_sell_high():
    price = _series([10, 10, 100, 100], "price")
    pv = _series([0, 0, 0, 0], "pv")
    bess = _bess(power=5, nameplate=10, rte=1.0, soc_init=0.05)
    ts = optimise_dispatch(pv, price, bess, GridSpec(50, 50, True)).timeseries
    assert ts.grid_bat.iloc[:2].sum() > 0
    assert ts.dis.iloc[2:].sum() > 0
    assert ts.dis.iloc[:2].sum() == pytest.approx(0, abs=1e-6)


def test_no_grid_charging_when_disallowed(summer_week):
    pv, price = summer_week
    ts = optimise_dispatch(pv, price, _bess(), GridSpec(30, 30, allow_grid_charging=False)).timeseries
    assert ts.grid_bat.abs().max() == pytest.approx(0, abs=1e-9)


def test_battery_never_loses_money_vs_baseline(summer_week):
    pv, price = summer_week
    base = pv_only_baseline(pv, price, 30.0)
    res = optimise_dispatch(pv, price, _bess(), GridSpec(30, 30, True))
    assert res.revenue_eur >= base.revenue_eur - 1e-3


def test_zero_size_battery_equals_baseline(summer_week):
    pv, price = summer_week
    tiny = BessSpec(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9)
    base = pv_only_baseline(pv, price, 30.0)
    res = optimise_dispatch(pv, price, tiny, GridSpec(30, 30, True))
    assert res.revenue_eur == pytest.approx(base.revenue_eur, rel=1e-6)


def test_cycle_limit_respected(summer_week):
    pv, price = summer_week
    bess = _bess(cycles=1.0)
    ts = optimise_dispatch(pv, price, bess, GridSpec(30, 30, True)).timeseries
    days = len(ts) / 24
    assert ts.dis.sum() <= bess.usable_mwh * days + 1e-6


def test_negative_prices_curtail_pv_in_baseline():
    pv = _series([10, 10, 10], "pv")
    price = _series([50, -5, 50], "price")
    ts = pv_only_baseline(pv, price, 30.0).timeseries
    assert ts.pv_grid.tolist() == [10, 0, 10]
    assert ts.curt.tolist() == [0, 10, 0]
