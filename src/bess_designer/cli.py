"""Command line interface.

    bess-designer design examples/brandenburg_50mwp.yaml --out examples/output
    bess-designer fetch-data examples/brandenburg_50mwp.yaml
    bess-designer demo-data examples/brandenburg_50mwp.yaml
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import load_config
from .data import PRICE_COL, PV_COL, fetch_pvgis_profile, fetch_smard_prices, write_series


def cmd_design(args) -> int:
    from .report import build_report, html_to_pdf
    from .sizing import run_study

    cfg = load_config(args.requirements)
    print(f"Study: {cfg.project.name}  ({len(cfg.options.power_mw) * len(cfg.options.duration_h)} options)")
    if cfg.data.synthetic:
        print("  NOTE: synthetic demo data in use (see README to fetch real data)")
    t0 = time.time()
    study = run_study(cfg, progress=lambda i, n, lab: print(f"  [{i}/{n}] {lab}"))
    rec = study.recommended
    print(f"Done in {time.time() - t0:.1f} s")
    print(f"Recommended: {rec.label}  ->  {rec.config.power_mw:g} MW / {rec.config.usable_mwh:.1f} MWh usable, "
          f"NPV {rec.kpis.npv_eur / 1e6:.2f} M€, payback {rec.kpis.simple_payback_years or float('nan'):.1f} a")
    path = build_report(study, args.out)
    print(f"Report: {path}")
    if args.pdf:
        try:
            print(f"PDF:    {html_to_pdf(path)}")
        except ImportError:
            print("PDF skipped: install playwright (pip install playwright && playwright install chromium) "
                  "or open the HTML file and print it to PDF.")
    return 0


def cmd_fetch(args) -> int:
    try:
        return _fetch(args)
    except Exception as e:  # noqa: BLE001  (network errors, API changes)
        print(f"Download failed: {e}\nCheck your internet connection and that PVGIS covers the year "
              "in the requirement file.", file=sys.stderr)
        return 1


def _fetch(args) -> int:
    cfg = load_config(args.requirements)
    out = Path(args.out_dir)
    y, loc, s = cfg.data.year, cfg.project.location, cfg.site
    print(f"PVGIS hourly PV output for {loc.name} ({loc.lat}, {loc.lon}), {y} ...")
    pv = fetch_pvgis_profile(loc.lat, loc.lon, y, s.pv_tilt_deg, s.pv_azimuth_deg, s.pv_system_loss_pct)
    pv_path = out / f"pv_profile_{loc.lat:.2f}_{loc.lon:.2f}_{y}.csv"
    write_series(pv, pv_path, PV_COL)
    print(f"  {len(pv)} hours, {pv.sum():.0f} kWh/kWp -> {pv_path}")
    print(f"SMARD day-ahead prices DE-LU, {y} ...")
    pr = fetch_smard_prices(y)
    pr_path = out / f"day_ahead_prices_DE_LU_{y}.csv"
    write_series(pr, pr_path, PRICE_COL)
    print(f"  {len(pr)} values, mean {pr.mean():.1f} €/MWh, {(pr < 0).sum()} negative -> {pr_path}")
    print("\nNow update the 'data' section of your requirement file:\n")
    print(f"data:\n  year: {y}\n  pv_profile_csv: {pv_path.as_posix()}\n  price_csv: {pr_path.as_posix()}\n"
          f"  label: PVGIS (EU JRC) + SMARD day-ahead DE-LU {y}\n  synthetic: false")
    return 0


def cmd_demo(args) -> int:
    from .synthetic import synthetic_pv_profile, synthetic_prices

    cfg = load_config(args.requirements)
    loc, s, y = cfg.project.location, cfg.site, cfg.data.year
    pv = synthetic_pv_profile(loc.lat, loc.lon, y, s.pv_tilt_deg, s.pv_azimuth_deg, s.pv_system_loss_pct)
    pr = synthetic_prices(pv, y)
    write_series(pv, cfg.data.pv_profile_csv, PV_COL)
    write_series(pr, cfg.data.price_csv, PRICE_COL)
    print(f"Synthetic demo data written to {cfg.data.pv_profile_csv} and {cfg.data.price_csv}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bess-designer", description="PV + BESS sizing and proposal tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("design", help="run the sizing study and write the technical proposal")
    d.add_argument("requirements", help="customer requirement YAML file")
    d.add_argument("--out", default="output", help="output folder (default: output)")
    d.add_argument("--pdf", action="store_true", help="also print the report to PDF (needs playwright)")
    d.set_defaults(func=cmd_design)

    f = sub.add_parser("fetch-data", help="download PVGIS PV profile and SMARD prices for the site and year")
    f.add_argument("requirements")
    f.add_argument("--out-dir", default="data/real")
    f.set_defaults(func=cmd_fetch)

    m = sub.add_parser("demo-data", help="(re)generate the synthetic demo data")
    m.add_argument("requirements")
    m.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
