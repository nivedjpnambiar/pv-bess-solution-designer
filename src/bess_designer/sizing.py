"""Run a full sizing study: every (power, duration) option is configured from
the catalogue, dispatched for one year and evaluated economically."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .catalogue import Configuration, configure, load_catalogue
from .config import StudyConfig
from .data import SiteSeries, load_site_series
from .dispatch import BessSpec, DispatchResult, GridSpec, optimise_dispatch, pv_only_baseline
from .economics import EconomicKpis, build_cashflows, capex, evaluate, Cashflows


@dataclass
class SiteStats:
    hours: int
    pv_energy_mwh: float
    specific_yield_kwh_per_kwp: float
    mean_price: float
    negative_price_hours: int
    pv_capture_price: float            # PV-weighted average price of the baseline sales
    baseline_revenue_eur: float
    baseline_curtailed_mwh: float
    curtailed_grid_limit_mwh: float
    curtailed_negative_price_mwh: float


@dataclass
class OptionResult:
    config: Configuration
    dispatch: DispatchResult
    cashflows: Cashflows
    kpis: EconomicKpis
    tech: dict[str, float]

    @property
    def label(self) -> str:
        return self.config.label


@dataclass
class StudyResult:
    cfg: StudyConfig
    series: SiteSeries
    site: SiteStats
    baseline: DispatchResult
    options: list[OptionResult]
    ranked: list[int] = field(default_factory=list)

    @property
    def recommended(self) -> OptionResult | None:
        return self.options[self.ranked[0]] if self.ranked else None

    def table(self) -> pd.DataFrame:
        rows = []
        for i, o in enumerate(self.options):
            k, t = o.kpis, o.tech
            rows.append({
                "option": o.label,
                "rank": self.ranked.index(i) + 1 if i in self.ranked else None,
                "power_mw": o.config.power_mw,
                "usable_mwh": round(o.config.usable_mwh, 1),
                "battery_units": f"{o.config.n_battery_units} x {o.config.battery_unit.id}",
                "pcs_units": f"{o.config.n_pcs_units} x {o.config.pcs_unit.id}",
                "capex_meur": k.capex_eur / 1e6,
                "year1_extra_revenue_keur": t["extra_revenue_eur"] / 1e3,
                "cycles_per_year": t["equivalent_full_cycles"],
                "curtailment_recovered_mwh": t["curtailment_recovered_mwh"],
                "npv_meur": k.npv_eur / 1e6,
                "irr_pct": k.irr * 100 if k.irr is not None else None,
                "payback_years": k.simple_payback_years,
                "lcos_eur_per_mwh": k.lcos_eur_per_mwh,
            })
        return pd.DataFrame(rows)


def site_stats(series: SiteSeries, baseline: DispatchResult, pv_dc_mwp: float, export_limit: float) -> SiteStats:
    dt = series.dt_h
    pv = series.pv_mw.to_numpy()
    pr = series.price.to_numpy()
    sold = baseline.timeseries["pv_grid"].to_numpy()
    neg = pr < 0
    curt_neg = float(pv[neg].sum() * dt)
    curt_lim = float(np.clip(pv[~neg] - export_limit, 0, None).sum() * dt)
    return SiteStats(
        hours=len(pv),
        pv_energy_mwh=float(pv.sum() * dt),
        specific_yield_kwh_per_kwp=float(pv.sum() * dt / pv_dc_mwp),
        mean_price=float(pr.mean()),
        negative_price_hours=int(neg.sum()),
        pv_capture_price=float((pr * sold).sum() / sold.sum()) if sold.sum() > 0 else float("nan"),
        baseline_revenue_eur=baseline.revenue_eur,
        baseline_curtailed_mwh=float(baseline.timeseries["curt"].sum() * dt),
        curtailed_grid_limit_mwh=curt_lim,
        curtailed_negative_price_mwh=curt_neg,
    )


def technical_kpis(res: DispatchResult, base: DispatchResult, cfgn: Configuration, dt: float) -> dict[str, float]:
    ts = res.timeseries
    dis = ts["dis"].to_numpy()
    charge = (ts["pv_bat"] + ts["grid_bat"]).to_numpy()
    pr = ts["price"].to_numpy()
    discharged = float(dis.sum() * dt)
    charged = float(charge.sum() * dt)
    return {
        "extra_revenue_eur": res.revenue_eur - base.revenue_eur,
        "discharged_mwh": discharged,
        "charged_mwh": charged,
        "pv_charged_mwh": float(ts["pv_bat"].sum() * dt),
        "grid_charged_mwh": float(ts["grid_bat"].sum() * dt),
        "equivalent_full_cycles": discharged / cfgn.usable_mwh if cfgn.usable_mwh else 0.0,
        "curtailment_recovered_mwh": float((base.timeseries["curt"].sum() - ts["curt"].sum()) * dt),
        "avg_discharge_price": float((pr * dis).sum() / dis.sum()) if dis.sum() > 0 else float("nan"),
        "avg_charge_price": float((pr * charge).sum() / charge.sum()) if charge.sum() > 0 else float("nan"),
        "simultaneous_mwh": float(np.minimum(charge, dis).sum() * dt),
    }


def evaluate_option(cfg: StudyConfig, series: SiteSeries, base: DispatchResult, conf: Configuration) -> OptionResult:
    b, e, s = cfg.battery, cfg.economics, cfg.site
    spec = BessSpec(
        power_mw=conf.power_mw,
        usable_mwh=conf.usable_mwh,
        nameplate_mwh=conf.nameplate_mwh,
        soc_min_mwh=conf.nameplate_mwh * b.soc_min,
        soc_max_mwh=conf.nameplate_mwh * b.soc_max,
        soc_initial_mwh=conf.nameplate_mwh * b.soc_initial,
        roundtrip_efficiency=b.roundtrip_efficiency,
        max_cycles_per_day=b.max_cycles_per_day,
    )
    grid = GridSpec(s.grid_export_limit_mw, s.grid_import_limit_mw, s.allow_grid_charging)
    res = optimise_dispatch(
        series.pv_mw, series.price, spec, grid, series.dt_h,
        degradation_cost_eur_per_mwh=b.degradation_cost_eur_per_mwh,
        grid_charging_fee_eur_per_mwh=e.grid_charging_fee_eur_per_mwh,
    )
    tech = technical_kpis(res, base, conf, series.dt_h)
    # grid charging fees are a real cash cost
    year1 = tech["extra_revenue_eur"] - tech["grid_charged_mwh"] * e.grid_charging_fee_eur_per_mwh
    # scale a part-year dataset to a full year
    year1 *= 8760 / (len(series.pv_mw) * series.dt_h)
    disch = tech["discharged_mwh"] * 8760 / (len(series.pv_mw) * series.dt_h)
    cx = capex(conf.nameplate_mwh, conf.power_mw, e.capex_energy_eur_per_kwh, e.capex_power_eur_per_kw)
    cf = build_cashflows(cx, year1, disch, e.project_life_years, b.annual_capacity_fade, e.opex_pct_of_capex)
    return OptionResult(conf, res, cf, evaluate(cf, e.discount_rate, conf.usable_mwh), tech)


def rank(options: list[OptionResult], by: str) -> list[int]:
    def key(i: int) -> float:
        k = options[i].kpis
        if by == "irr":
            return -(k.irr if k.irr is not None else -math.inf)
        if by == "payback":
            return k.simple_payback_years if k.simple_payback_years is not None else math.inf
        return -k.npv_eur
    return sorted(range(len(options)), key=key)


def sensitivity(study: StudyResult, option: OptionResult | None = None) -> pd.DataFrame:
    """How robust is the recommendation? Re-evaluate one option under changed
    assumptions. CAPEX cases only change the cash flows; the others re-run the
    dispatch."""
    import copy

    option = option or study.recommended
    cfg = study.cfg
    base_k = option.kpis
    rows = [("Base case", option)]

    def with_changes(fn) -> OptionResult:
        c = copy.deepcopy(cfg)
        fn(c)
        return evaluate_option(c, study.series, study.baseline, option.config)

    def capex_case(factor: float) -> OptionResult:
        c = copy.deepcopy(cfg)
        c.economics.capex_energy_eur_per_kwh *= factor
        c.economics.capex_power_eur_per_kw *= factor
        cx = option.cashflows.capex_eur * factor
        cf = build_cashflows(cx, option.cashflows.revenue_eur[0], option.cashflows.discharged_mwh[0],
                             c.economics.project_life_years, c.battery.annual_capacity_fade,
                             c.economics.opex_pct_of_capex)
        return OptionResult(option.config, option.dispatch, cf,
                            evaluate(cf, c.economics.discount_rate, option.config.usable_mwh), option.tech)

    rows.append(("CAPEX -20 %", capex_case(0.8)))
    rows.append(("CAPEX +20 %", capex_case(1.2)))
    if cfg.site.allow_grid_charging:
        rows.append(("No grid charging (PV only)", with_changes(lambda c: setattr(c.site, "allow_grid_charging", False))))
    rows.append(("Max 1 cycle per day", with_changes(lambda c: setattr(c.battery, "max_cycles_per_day", 1.0))))
    rows.append(("Round-trip efficiency 0.85", with_changes(lambda c: setattr(c.battery, "roundtrip_efficiency", 0.85))))

    out = []
    for name, o in rows:
        k = o.kpis
        out.append({
            "case": name,
            "year1_extra_revenue_keur": o.tech["extra_revenue_eur"] / 1e3,
            "cycles_per_year": o.tech["equivalent_full_cycles"],
            "npv_meur": k.npv_eur / 1e6,
            "npv_change_meur": (k.npv_eur - base_k.npv_eur) / 1e6,
            "irr_pct": k.irr * 100 if k.irr is not None else None,
            "payback_years": k.simple_payback_years,
        })
    return pd.DataFrame(out)


def run_study(cfg: StudyConfig, series: SiteSeries | None = None, progress=None) -> StudyResult:
    series = series or load_site_series(cfg)
    cat = load_catalogue(cfg.catalogue_path)
    base = pv_only_baseline(series.pv_mw, series.price, cfg.site.grid_export_limit_mw, series.dt_h)
    stats = site_stats(series, base, cfg.site.pv_dc_mwp, cfg.site.grid_export_limit_mw)

    combos = list(itertools.product(cfg.options.power_mw, cfg.options.duration_h))
    results = []
    for i, (p, d) in enumerate(combos):
        conf = configure(p, d, cat, cfg.battery.soc_min, cfg.battery.soc_max)
        results.append(evaluate_option(cfg, series, base, conf))
        if progress:
            progress(i + 1, len(combos), conf.label)
    study = StudyResult(cfg, series, stats, base, results)
    study.ranked = rank(results, cfg.options.rank_by)
    return study
