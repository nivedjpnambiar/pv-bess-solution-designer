"""Build the customer-facing technical proposal (self-contained HTML)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
from jinja2 import Environment, PackageLoader, select_autoescape

from . import charts
from .sizing import StudyResult, sensitivity


def _fmt(v, nd=1, unit="", none="n/a"):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return none
    return f"{v:,.{nd}f}{unit}".replace(",", " ")


def _reasoning(study: StudyResult) -> list[str]:
    """Plain-language arguments for the recommendation, derived from the results."""
    rec = study.recommended
    opts = study.options
    out = []
    by = {"npv": "net present value", "irr": "internal rate of return", "payback": "payback time"}
    out.append(f"{rec.label} gives the best {by[study.cfg.options.rank_by]} of the "
               f"{len(opts)} options studied.")
    if len(study.ranked) > 1:
        r2 = opts[study.ranked[1]]
        out.append(f"Runner-up is {r2.label} (NPV {_fmt(r2.kpis.npv_eur / 1e6, 2)} M€ vs "
                   f"{_fmt(rec.kpis.npv_eur / 1e6, 2)} M€).")

    durations = sorted({o.config.target_duration_h for o in opts})
    if len(durations) > 1:
        parts = []
        for d in durations:
            sel = [o for o in opts if o.config.target_duration_h == d]
            per_mwh = np.mean([o.tech["extra_revenue_eur"] / o.config.usable_mwh for o in sel]) / 1e3
            parts.append(f"{d:g} h: {per_mwh:.0f} k€")
        out.append("Revenue per usable MWh and year falls as the duration grows ("
                   + ", ".join(parts) + "), because the extra hours of storage are needed on fewer "
                   "days. This is why longer systems earn more in total but not more per euro invested.")

    max_p = max(o.config.target_power_mw for o in opts)
    if rec.config.target_power_mw == max_p and len({o.config.target_power_mw for o in opts}) > 1:
        lim = study.cfg.site.grid_export_limit_mw
        if rec.config.power_mw >= 0.9 * lim:
            out.append(f"Value still grows with power up to the largest size studied. The practical limit is "
                       f"the {lim:g} MW grid connection, which the BESS shares with the PV plant; a larger "
                       "system would need more grid capacity.")
        else:
            out.append("The best option is the largest power studied, so the option range should be "
                       "extended before a final decision.")

    b = study.cfg.battery
    if b.max_cycles_per_day is not None:
        limit = b.max_cycles_per_day * 365
        if rec.tech["equivalent_full_cycles"] >= 0.98 * limit:
            out.append(f"The recommended system reaches the cycle limit of {b.max_cycles_per_day:g} "
                       "cycles per day, so the warranty terms of the battery directly limit revenue.")
    share_pv = rec.tech["pv_charged_mwh"] / rec.tech["charged_mwh"] if rec.tech["charged_mwh"] else 0
    out.append(f"{share_pv * 100:.0f} % of the charging energy comes from the PV plant, the rest from "
               "the grid in low-price hours. Whether grid charging is allowed for the site (EEG rules "
               "for mixed storage) has a large effect; see the sensitivity table.")
    return out


def _assumptions(study: StudyResult) -> list[str]:
    c = study.cfg
    b, e = c.battery, c.economics
    return [
        f"Weather and prices of one historical year ({c.data.year}); data: {c.data.label}.",
        "Perfect foresight of day-ahead prices for the whole year. Real trading earns less, "
        "so revenues are an upper bound for the day-ahead market alone.",
        "Only day-ahead arbitrage and PV shifting are valued. Balancing services (FCR, aFRR), "
        "intraday trading and capacity payments are not included.",
        "Price taker: the BESS does not change market prices. This makes revenue grow almost "
        "linearly with size, which overstates the value of very large systems.",
        "AC-coupled BESS. PV energy lost by DC/AC inverter clipping is not recoverable "
        "(a DC-coupled design could recover part of it).",
        "In hours with negative prices the PV plant is curtailed (no payment for feed-in).",
        f"Round-trip efficiency {b.roundtrip_efficiency:.0%} (AC to AC), usable state of charge "
        f"{b.soc_min:.0%} to {b.soc_max:.0%}, max {b.max_cycles_per_day} cycles per day.",
        f"Capacity fade {b.annual_capacity_fade:.1%} per year, revenue scales with it. No augmentation.",
        f"CAPEX {e.capex_energy_eur_per_kwh:.0f} €/kWh (nameplate) + {e.capex_power_eur_per_kw:.0f} €/kW, "
        f"OPEX {e.opex_pct_of_capex:.1%} of CAPEX per year, {e.project_life_years} years, "
        f"discount rate {e.discount_rate:.1%}. No taxes, financing or price escalation.",
        f"Grid charging fee {e.grid_charging_fee_eur_per_mwh:.0f} €/MWh.",
        "Product catalogue is generic and illustrative; values are not from a specific vendor.",
    ]


def build_report(study: StudyResult, out_dir: str | Path, save_pngs: bool = True) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = study.recommended
    table = study.table()

    figs = {
        "npv": charts.option_npv_chart(table, rec.label),
        "dispatch": charts.dispatch_week_chart(
            rec.dispatch.timeseries, study.cfg.site.grid_export_limit_mw,
            charts.pick_example_window(rec.dispatch.timeseries)),
        "monthly": charts.monthly_chart(rec.dispatch.timeseries, study.baseline.timeseries, study.series.dt_h),
        "prices": charts.price_duration_chart(study.series.price, study.site.pv_capture_price),
    }
    if save_pngs:
        for k, f in figs.items():
            f.savefig(out_dir / f"chart_{k}.png", dpi=150, bbox_inches="tight", facecolor=charts.SURFACE)
    imgs = {k: charts.to_png_b64(f) for k, f in figs.items()}

    sens = sensitivity(study)
    env = Environment(loader=PackageLoader("bess_designer", "templates"), autoescape=select_autoescape(["html"]))
    env.filters["f"] = _fmt
    env.filters["g"] = lambda v: f"{v:g}"
    html = env.get_template("report.html").render(
        s=study, cfg=study.cfg, rec=rec, table=table.sort_values("rank").to_dict("records"),
        sens=sens.to_dict("records"), imgs=imgs, reasoning=_reasoning(study),
        assumptions=_assumptions(study), today=dt.date.today().strftime("%d.%m.%Y"),
    )
    path = out_dir / "technical_proposal.html"
    path.write_text(html, encoding="utf-8")
    table.sort_values("rank").to_csv(out_dir / "options.csv", index=False, float_format="%.3f")
    sens.to_csv(out_dir / "sensitivity.csv", index=False, float_format="%.3f")
    return path


def html_to_pdf(html_path: str | Path, pdf_path: str | Path | None = None) -> Path:
    """Print the HTML report to PDF with a headless Chromium (needs ``playwright``)."""
    from playwright.sync_api import sync_playwright

    html_path = Path(html_path).resolve()
    pdf_path = Path(pdf_path or html_path.with_suffix(".pdf"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.pdf(path=str(pdf_path), format="A4", print_background=True,
                 margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
        browser.close()
    return pdf_path
