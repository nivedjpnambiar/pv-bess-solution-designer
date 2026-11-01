"""Interactive front end: change the customer requirements and see the
recommended BESS, the option comparison and the dispatch.

    streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import copy
import io
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bess_designer import charts  # noqa: E402
from bess_designer.config import load_config, validate  # noqa: E402
from bess_designer.data import PRICE_COL, PV_COL, build_site_series, read_series  # noqa: E402
from bess_designer.report import build_report  # noqa: E402
from bess_designer.sizing import run_study, sensitivity  # noqa: E402

st.set_page_config(page_title="PV + BESS Solution Designer", layout="wide")

EXAMPLES = {p.stem: p for p in sorted((ROOT / "examples").glob("*.yaml"))}


@st.cache_data(show_spinner=False)
def _read(path_or_bytes, column: str) -> pd.Series:
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    return read_series(src, column)


def _list(text: str) -> list[float]:
    return [float(x) for x in text.replace(";", ",").split(",") if x.strip()]


st.title("PV + BESS Solution Designer")
st.caption("Sizing and technical-economic screening of battery storage for a solar park")

with st.sidebar:
    st.header("Customer requirements")
    example = st.selectbox("Start from example", list(EXAMPLES))
    base_cfg = load_config(EXAMPLES[example])
    cfg = copy.deepcopy(base_cfg)

    st.subheader("Site")
    cfg.site.pv_dc_mwp = st.number_input("PV size DC [MWp]", 1.0, 1000.0, float(base_cfg.site.pv_dc_mwp), 1.0)
    cfg.site.pv_ac_mw = st.number_input("PV inverter AC [MW]", 1.0, 1000.0, float(base_cfg.site.pv_ac_mw), 1.0)
    cfg.site.grid_export_limit_mw = st.number_input("Grid connection export [MW]", 1.0, 1000.0,
                                                    float(base_cfg.site.grid_export_limit_mw), 1.0)
    cfg.site.allow_grid_charging = st.toggle("Allow grid charging", base_cfg.site.allow_grid_charging)
    if cfg.site.allow_grid_charging:
        cfg.site.grid_import_limit_mw = st.number_input(
            "Grid connection import [MW]", 0.0, 1000.0, float(max(base_cfg.site.grid_import_limit_mw, 1.0)), 1.0)

    st.subheader("BESS options")
    p_txt = st.text_input("Power options [MW]", ", ".join(f"{x:g}" for x in base_cfg.options.power_mw))
    d_txt = st.text_input("Duration options [h]", ", ".join(f"{x:g}" for x in base_cfg.options.duration_h))
    cfg.options.rank_by = st.selectbox("Rank options by", ["npv", "irr", "payback"],
                                       index=["npv", "irr", "payback"].index(base_cfg.options.rank_by))

    st.subheader("Battery")
    cfg.battery.roundtrip_efficiency = st.slider("Round-trip efficiency", 0.75, 0.95,
                                                 float(base_cfg.battery.roundtrip_efficiency), 0.01)
    cfg.battery.max_cycles_per_day = st.slider("Max cycles per day", 0.5, 3.0,
                                               float(base_cfg.battery.max_cycles_per_day or 2.0), 0.1)

    st.subheader("Economics")
    cfg.economics.capex_energy_eur_per_kwh = st.number_input(
        "CAPEX energy [€/kWh]", 50.0, 600.0, float(base_cfg.economics.capex_energy_eur_per_kwh), 5.0)
    cfg.economics.capex_power_eur_per_kw = st.number_input(
        "CAPEX power [€/kW]", 0.0, 400.0, float(base_cfg.economics.capex_power_eur_per_kw), 5.0)
    cfg.economics.discount_rate = st.slider("Discount rate", 0.02, 0.15, float(base_cfg.economics.discount_rate), 0.005)

    st.subheader("Data")
    pv_up = st.file_uploader(f"PV profile CSV ({PV_COL})", type="csv")
    pr_up = st.file_uploader(f"Price CSV ({PRICE_COL})", type="csv")
    run = st.button("Run study", type="primary", width="stretch")

try:
    cfg.options.power_mw, cfg.options.duration_h = _list(p_txt), _list(d_txt)
    validate(cfg)
except Exception as e:  # noqa: BLE001
    st.error(f"Input problem: {e}")
    st.stop()

pv = _read(pv_up.getvalue() if pv_up else str(cfg.data.pv_profile_csv), PV_COL)
pr = _read(pr_up.getvalue() if pr_up else str(cfg.data.price_csv), PRICE_COL)
if pv_up or pr_up:
    cfg.data.synthetic = False
    cfg.data.label = "Uploaded data"
series = build_site_series(pv, pr, cfg.site.pv_dc_mwp, cfg.site.pv_ac_mw, cfg.data.year)

if cfg.data.synthetic:
    st.warning("Demo mode: synthetic weather and price data. Upload real PVGIS / SMARD files "
               "(see README) for a real assessment.")

if not run and "study" not in st.session_state:
    st.info("Set the requirements in the sidebar and press **Run study**.")
    st.stop()

if run:
    bar = st.progress(0.0, "Optimising dispatch ...")
    st.session_state.study = run_study(cfg, series, progress=lambda i, n, lab: bar.progress(i / n, f"{lab}"))
    bar.progress(1.0, "Sensitivity and report ...")
    st.session_state.sens = sensitivity(st.session_state.study)
    with tempfile.TemporaryDirectory() as tmp:
        st.session_state.html = build_report(st.session_state.study, tmp, save_pngs=False).read_text(encoding="utf-8")
    bar.empty()

study = st.session_state.study
rec = study.recommended

st.subheader(f"Recommendation: {rec.config.power_mw:g} MW / {rec.config.usable_mwh:.1f} MWh usable")
st.caption(f"{rec.config.n_battery_units} × {rec.config.battery_unit.id} + "
           f"{rec.config.n_pcs_units} × {rec.config.pcs_unit.id}")
c = st.columns(5)
c[0].metric("CAPEX", f"{rec.kpis.capex_eur / 1e6:.1f} M€")
c[1].metric("Extra revenue year 1", f"{rec.tech['extra_revenue_eur'] / 1e3:,.0f} k€")
c[2].metric("NPV", f"{rec.kpis.npv_eur / 1e6:.2f} M€")
c[3].metric("Payback", f"{rec.kpis.simple_payback_years:.1f} a" if rec.kpis.simple_payback_years else "n/a")
c[4].metric("Cycles per year", f"{rec.tech['equivalent_full_cycles']:.0f}")

tab1, tab2, tab3, tab4 = st.tabs(["Options", "Dispatch", "Sensitivity", "Site & market"])
table = study.table().sort_values("rank")
with tab1:
    st.pyplot(charts.option_npv_chart(table, rec.label))
    st.dataframe(table.round(2), hide_index=True, width="stretch")
with tab2:
    ts = rec.dispatch.timeseries
    default_start = charts.pick_example_window(ts).tz_convert("Europe/Berlin").date()
    day = st.date_input("Start day", default_start, min_value=ts.index[0].date(), max_value=ts.index[-1].date())
    start = pd.Timestamp(day, tz="Europe/Berlin").tz_convert("UTC")
    st.pyplot(charts.dispatch_week_chart(ts, cfg.site.grid_export_limit_mw, start))
    st.pyplot(charts.monthly_chart(ts, study.baseline.timeseries, study.series.dt_h))
with tab3:
    st.dataframe(st.session_state.sens.round(2), hide_index=True, width="stretch")
with tab4:
    s = study.site
    m = st.columns(4)
    m[0].metric("PV energy", f"{s.pv_energy_mwh:,.0f} MWh/a")
    m[1].metric("Average price", f"{s.mean_price:.1f} €/MWh")
    m[2].metric("PV capture price", f"{s.pv_capture_price:.1f} €/MWh")
    m[3].metric("Negative price hours", f"{s.negative_price_hours}")
    st.pyplot(charts.price_duration_chart(study.series.price, s.pv_capture_price))

st.download_button("Download technical proposal (HTML)", st.session_state.html,
                   "technical_proposal.html", "text/html")
