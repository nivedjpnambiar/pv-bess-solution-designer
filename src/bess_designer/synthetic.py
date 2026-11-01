"""Synthetic demo data so the repository runs offline.

THIS IS NOT REAL MARKET OR WEATHER DATA. It is only used for the demo and the
tests. For any real study, run ``bess-designer fetch-data`` to download PVGIS
and SMARD data (see README). Reports built on synthetic data carry a banner.

PV:     pvlib clear-sky irradiance on a tilted plane, multiplied by a random
        daily cloudiness factor with a seasonal mean.
Prices: a seasonal base level plus a daily shape (morning and evening peaks),
        reduced in sunny and windy hours (merit-order effect) and on weekends,
        plus noise. Tuned to look roughly like a recent German year
        (mean around 90 EUR/MWh, a few hundred negative hours).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_pv_profile(lat: float, lon: float, year: int, tilt: float = 25.0,
                         azimuth: float = 180.0, loss_pct: float = 14.0, seed: int = 7) -> pd.Series:
    import pvlib

    rng = np.random.default_rng(seed)
    idx = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", tz="UTC", inclusive="left")
    loc = pvlib.location.Location(lat, lon, tz="UTC")
    mid = idx + pd.Timedelta(minutes=30)  # irradiance at the middle of the hour
    cs = loc.get_clearsky(mid, model="ineichen")
    sp = loc.get_solarposition(mid)
    poa = pvlib.irradiance.get_total_irradiance(
        tilt, azimuth, sp["apparent_zenith"], sp["azimuth"], cs["dni"], cs["ghi"], cs["dhi"]
    )["poa_global"].fillna(0.0).to_numpy()

    days = idx.normalize().unique()
    doy = days.dayofyear.to_numpy()
    mean_clear = 0.52 + 0.20 * np.sin(2 * np.pi * (doy - 80) / 365)  # sunnier in summer
    a = 2.2
    daily = rng.beta(a * mean_clear / (1 - mean_clear), a, size=len(days))
    day_factor = pd.Series(daily, index=days).reindex(idx.normalize()).to_numpy()
    hourly_noise = np.clip(1 + rng.normal(0, 0.12, len(idx)), 0.4, 1.15)
    perf = (1 - loss_pct / 100)
    pv = np.clip(poa / 1000.0 * day_factor * hourly_noise * perf, 0, 1.0)
    return pd.Series(pv, index=idx, name="pv_kw_per_kwp")


def synthetic_prices(pv_index_series: pd.Series, year: int, seed: int = 11) -> pd.Series:
    """Prices that react to a (Germany-wide) solar signal."""
    rng = np.random.default_rng(seed)
    idx = pv_index_series.index
    local = idx.tz_convert("Europe/Berlin")
    month_base = {1: 112, 2: 118, 3: 104, 4: 98, 5: 86, 6: 92, 7: 82, 8: 92, 9: 100, 10: 92, 11: 98, 12: 84}
    base = np.array([month_base[m] for m in local.month], dtype=float)

    h = local.hour.to_numpy()
    shape = (
        24 * np.exp(-0.5 * ((h - 8) / 1.5) ** 2)
        + 42 * np.exp(-0.5 * ((h - 19) / 1.8) ** 2)
        - 18 * np.exp(-0.5 * ((h - 3.5) / 2.0) ** 2)
    )
    weekend = np.where(local.dayofweek.to_numpy() >= 5, -22.0, 0.0)

    # Germany-wide solar: smoothed version of the site profile
    solar = pv_index_series.rolling(3, center=True, min_periods=1).mean().to_numpy()
    solar = solar / max(solar.max(), 1e-9)

    days = idx.normalize().unique()
    wind_day = rng.gamma(1.6, 0.35, size=len(days))
    winter = 1 + 0.5 * np.cos(2 * np.pi * (days.dayofyear.to_numpy() - 15) / 365)
    wind = pd.Series(wind_day * winter, index=days).reindex(idx.normalize()).to_numpy()
    wind = pd.Series(wind).rolling(12, min_periods=1).mean().to_numpy()

    noise = np.zeros(len(idx))
    eps = rng.normal(0, 9, len(idx))
    for t in range(1, len(idx)):
        noise[t] = 0.85 * noise[t - 1] + eps[t]

    price = base + shape + weekend - 135 * solar ** 1.4 - 34 * (wind - wind.mean()) + noise
    spikes = rng.random(len(idx)) < 0.004
    price[spikes] += rng.gamma(2.0, 80.0, spikes.sum())
    price = np.clip(price, -250, 900)
    return pd.Series(np.round(price, 2), index=idx, name="price_eur_per_mwh")
