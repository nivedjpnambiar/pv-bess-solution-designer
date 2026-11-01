"""Catalogue configuration, economics and an end-to-end run on the demo data."""

from pathlib import Path

import numpy as np
import pytest

from bess_designer.catalogue import DEFAULT_CATALOGUE, configure
from bess_designer.config import ConfigError, load_config
from bess_designer.economics import build_cashflows, irr, lcos, npv, simple_payback
from bess_designer.report import build_report
from bess_designer.sizing import run_study

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "brandenburg_50mwp.yaml"


def test_configure_meets_target_with_whole_units():
    c = configure(10, 2, DEFAULT_CATALOGUE, soc_min=0.05, soc_max=0.95)
    assert c.usable_mwh >= 20.0
    assert c.power_mw >= 10.0
    assert c.n_battery_units == 5          # 20 / 0.9 = 22.2 MWh -> 5 x 5 MWh
    assert c.n_pcs_units == 4              # 10 / 2.5
    assert c.nameplate_mwh == pytest.approx(25.0)


def test_npv_irr_payback_consistent():
    net = np.full(10, 150.0)
    assert npv(0.0, 1000, net) == pytest.approx(500.0)
    r = irr(1000, net)
    assert npv(r, 1000, net) == pytest.approx(0.0, abs=1e-3)
    assert simple_payback(1000, net) == pytest.approx(1000 / 150)


def test_payback_none_if_never_repaid():
    assert simple_payback(1000, np.full(5, 100.0)) is None


def test_capacity_fade_reduces_revenue():
    cf = build_cashflows(1e6, 2e5, 1000, 10, annual_fade=0.02, opex_pct_of_capex=0.01)
    assert cf.revenue_eur[0] == pytest.approx(2e5)
    assert cf.revenue_eur[-1] == pytest.approx(2e5 * (1 - 0.02 * 9))
    assert lcos(0.07, cf) > 0


def test_invalid_config_rejected(tmp_path):
    bad = EXAMPLE.read_text().replace("soc_min: 0.05", "soc_min: 0.99")
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    # relative data paths would not resolve from tmp, but validation fails first
    with pytest.raises(ConfigError):
        load_config(p)


@pytest.fixture(scope="module")
def small_study():
    cfg = load_config(EXAMPLE)
    cfg.options.power_mw = [10, 20]
    cfg.options.duration_h = [1, 2]
    return run_study(cfg)


def test_end_to_end_study(small_study):
    s = small_study
    assert len(s.options) == 4
    assert s.recommended is not None
    # ranking by NPV is descending
    npvs = [s.options[i].kpis.npv_eur for i in s.ranked]
    assert npvs == sorted(npvs, reverse=True)
    # every option at least as good as PV only in year-1 revenue
    assert all(o.tech["extra_revenue_eur"] >= -1 for o in s.options)
    # the LP should not charge and discharge at the same time in any meaningful amount
    assert all(o.tech["simultaneous_mwh"] < 0.01 * o.tech["discharged_mwh"] + 1 for o in s.options)


def test_report_is_written(small_study, tmp_path):
    path = build_report(small_study, tmp_path)
    html = path.read_text(encoding="utf-8")
    assert "Recommendation" in html and "data:image/png;base64" in html
    assert (tmp_path / "options.csv").exists()
