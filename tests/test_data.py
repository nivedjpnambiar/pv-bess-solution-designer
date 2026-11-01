"""Input alignment and the SMARD parser (with a fake HTTP session, no internet needed)."""

import numpy as np
import pandas as pd
import pytest

from bess_designer.data import build_site_series, fetch_smard_prices


def test_build_site_series_scales_and_clips():
    idx = pd.date_range("2023-01-01", periods=24 * 10, freq="h", tz="UTC")
    pv = pd.Series(np.full(len(idx), 0.9), index=idx)
    pr = pd.Series(np.full(len(idx), 50.0), index=idx)
    s = build_site_series(pv, pr, pv_dc_mwp=50, pv_ac_mw=40)
    assert s.pv_mw.max() == pytest.approx(40.0)  # 0.9 * 50 = 45 clipped to 40


def test_quarter_hour_prices_are_averaged():
    idx_h = pd.date_range("2023-01-01", periods=24 * 8, freq="h", tz="UTC")
    idx_q = pd.date_range("2023-01-01", periods=24 * 8 * 4, freq="15min", tz="UTC")
    pv = pd.Series(0.5, index=idx_h)
    pr = pd.Series(np.tile([10.0, 20.0, 30.0, 40.0], 24 * 8), index=idx_q)
    s = build_site_series(pv, pr, 10, 10)
    assert s.price.iloc[0] == pytest.approx(25.0)
    assert len(s.price) == len(idx_h)


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _FakeSmard:
    """Mimics the SMARD chart API: an index of weekly chunks and the chunks."""

    def __init__(self):
        start = pd.Timestamp("2022-12-26", tz="UTC")
        self.weeks = [start + pd.Timedelta(weeks=i) for i in range(60)]

    def get(self, url, timeout=30):
        if url.endswith("index_hour.json"):
            return _Resp({"timestamps": [int(w.value // 10**6) for w in self.weeks]})
        ts = int(url.rsplit("_", 1)[1].split(".")[0])
        t0 = pd.Timestamp(ts, unit="ms", tz="UTC")
        hours = pd.date_range(t0, periods=168, freq="h")
        series = [[int(h.value // 10**6), (None if h.year > 2023 else 42.0)] for h in hours]
        return _Resp({"series": series})


def test_fetch_smard_prices_parses_one_year():
    s = fetch_smard_prices(2023, session=_FakeSmard())
    assert s.index.min() == pd.Timestamp("2023-01-01", tz="UTC")
    assert s.index.max() == pd.Timestamp("2023-12-31 23:00", tz="UTC")
    assert len(s) == 8760
    assert (s == 42.0).all()
