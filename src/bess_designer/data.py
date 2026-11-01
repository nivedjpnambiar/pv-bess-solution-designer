"""Input time series: PV generation profile and day-ahead prices.

Both series are stored as simple CSV files with a UTC timestamp column so that
studies are reproducible and the data can be inspected in Excel.

* PV profile:   ``timestamp_utc, pv_kw_per_kwp``   (AC output per kWp DC)
* Prices:       ``timestamp_utc, price_eur_per_mwh``  (DE-LU day-ahead)

``fetch_pvgis_profile`` and ``fetch_smard_prices`` download real data from
PVGIS (EU JRC) and SMARD (Bundesnetzagentur). They need internet access.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PV_COL = "pv_kw_per_kwp"
PRICE_COL = "price_eur_per_mwh"
TS_COL = "timestamp_utc"

SMARD_BASE = "https://www.smard.de/app/chart_data"
SMARD_FILTER_DAY_AHEAD_DE_LU = 4169


@dataclass
class SiteSeries:
    """Hourly series for one site, aligned on a common UTC index."""

    pv_mw: pd.Series        # AC PV power available at the inverter output, MW
    price: pd.Series        # day-ahead price, EUR/MWh
    dt_h: float             # time step in hours

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.pv_mw.index


# --------------------------------------------------------------------------- IO

def read_series(path: str | Path, column: str) -> pd.Series:
    df = pd.read_csv(path)
    if TS_COL not in df.columns or column not in df.columns:
        raise ValueError(f"{path} must have columns '{TS_COL}' and '{column}'")
    idx = pd.to_datetime(df[TS_COL], utc=True)
    s = pd.Series(df[column].to_numpy(dtype=float), index=idx, name=column)
    s = s[~s.index.duplicated(keep="first")].sort_index()
    return s


def write_series(s: pd.Series, path: str | Path, column: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame({TS_COL: s.index.strftime("%Y-%m-%dT%H:%M:%SZ"), column: s.to_numpy()})
    out.to_csv(path, index=False, float_format="%.4f")


def build_site_series(
    pv_per_kwp: pd.Series,
    price: pd.Series,
    pv_dc_mwp: float,
    pv_ac_mw: float,
    year: int | None = None,
) -> SiteSeries:
    """Align PV profile and prices on one hourly UTC index and scale the PV
    profile to the plant size. AC output is clipped at the inverter rating."""
    pv_per_kwp = pv_per_kwp.copy()
    price = price.copy()
    pv_per_kwp.index = pv_per_kwp.index.floor("h")
    price.index = price.index.floor("h")
    # Prices after Oct 2025 can be quarter-hourly; average them to hourly.
    price = price.groupby(level=0).mean()
    pv_per_kwp = pv_per_kwp.groupby(level=0).mean()

    if year is not None:
        pv_per_kwp = pv_per_kwp[pv_per_kwp.index.year == year]
        price = price[price.index.year == year]

    idx = pv_per_kwp.index.intersection(price.index)
    if len(idx) < 24 * 7:
        raise ValueError(
            "PV profile and price series overlap by less than one week. "
            "Check that both files cover the same year."
        )
    full = pd.date_range(idx.min(), idx.max(), freq="h", tz="UTC")
    pv = pv_per_kwp.reindex(full).interpolate(limit=3).fillna(0.0).clip(lower=0.0)
    pr = price.reindex(full).interpolate(limit=3)
    if pr.isna().any():
        raise ValueError(f"Price series has {int(pr.isna().sum())} missing hours that could not be filled")

    pv_mw = (pv * pv_dc_mwp).clip(upper=pv_ac_mw)  # kW/kWp * MWp = MW
    pv_mw.name = "pv_mw"
    pr.name = "price"
    return SiteSeries(pv_mw=pv_mw, price=pr, dt_h=1.0)


def load_site_series(cfg) -> SiteSeries:
    """Load both CSV files referenced in a StudyConfig."""
    pv = read_series(cfg.data.pv_profile_csv, PV_COL)
    pr = read_series(cfg.data.price_csv, PRICE_COL)
    return build_site_series(pv, pr, cfg.site.pv_dc_mwp, cfg.site.pv_ac_mw, cfg.data.year)


# ------------------------------------------------------------------ fetchers

def fetch_pvgis_profile(
    lat: float,
    lon: float,
    year: int,
    tilt_deg: float = 25.0,
    azimuth_deg: float = 180.0,
    loss_pct: float = 14.0,
) -> pd.Series:
    """Hourly PV output per kWp from PVGIS (EU JRC) for one historical year.

    Uses ``pvlib.iotools.get_pvgis_hourly``. PVGIS reports the PV system power
    ``P`` in W for the requested peak power (1 kWp here), so P / 1000 is the
    output in kW per kWp. PVGIS timestamps are UTC with a minute offset
    (e.g. 10:10); they are floored to the full hour.
    """
    from pvlib.iotools import get_pvgis_hourly

    res = get_pvgis_hourly(
        latitude=lat,
        longitude=lon,
        start=year,
        end=year,
        pvcalculation=True,
        peakpower=1.0,
        loss=loss_pct,
        surface_tilt=tilt_deg,
        surface_azimuth=azimuth_deg,
        components=False,
        map_variables=True,
        timeout=60,
    )
    data = res[0]
    s = (data["P"] / 1000.0).clip(lower=0.0)
    s.index = s.index.floor("h")
    s.name = PV_COL
    return s


def fetch_smard_prices(year: int, resolution: str = "hour", session=None) -> pd.Series:
    """DE-LU day-ahead prices for one calendar year from SMARD.

    The SMARD chart API serves weekly chunks: ``index_<resolution>.json`` lists
    the chunk start timestamps (ms since epoch), and each chunk file holds
    ``{"series": [[ms, value], ...]}``. Values are EUR/MWh; future or missing
    values are ``null``.
    """
    import requests

    sess = session or requests.Session()
    f = SMARD_FILTER_DAY_AHEAD_DE_LU
    idx_url = f"{SMARD_BASE}/{f}/DE/index_{resolution}.json"
    r = sess.get(idx_url, timeout=30)
    r.raise_for_status()
    stamps = sorted(r.json()["timestamps"])

    start = pd.Timestamp(f"{year}-01-01", tz="UTC")
    end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
    week_ms = 7 * 24 * 3600 * 1000
    wanted = [
        t for t in stamps
        if t + week_ms >= start.value // 10**6 and t < end.value // 10**6
    ]
    if not wanted:
        raise ValueError(f"SMARD has no price data for {year}")

    rows: list[tuple[int, float]] = []
    for t in wanted:
        url = f"{SMARD_BASE}/{f}/DE/{f}_DE_{resolution}_{t}.json"
        rr = sess.get(url, timeout=30)
        rr.raise_for_status()
        rows.extend((ms, v) for ms, v in rr.json().get("series", []) if v is not None)

    ms, vals = zip(*rows)
    s = pd.Series(vals, index=pd.to_datetime(np.array(ms), unit="ms", utc=True), name=PRICE_COL)
    s = s[~s.index.duplicated()].sort_index()
    s = s[(s.index >= start) & (s.index < end)]
    return s.astype(float)
