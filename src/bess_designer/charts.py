"""Static charts for the technical proposal (matplotlib, PNG).

Colour roles follow a fixed categorical order (slot 1 blue, 2 orange, 3 aqua,
4 yellow) and every chart has a single y-axis; related measures with
different units go into stacked panels instead of a second axis.
"""

from __future__ import annotations

import io
from base64 import b64encode

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT2 = "#52514e"
GRID = "#e4e3df"
S1, S2, S3, S4 = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"  # categorical slots 1-4
NEUTRAL = "#a8a7a1"

ROLE = {
    "dis": (S1, "BESS discharge to grid"),
    "pv_grid": (S2, "PV sold to grid"),
    "pv_bat": (S3, "PV charging BESS"),
    "grid_bat": (S4, "Grid charging BESS"),
}

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.edgecolor": GRID,
    "axes.labelcolor": TEXT2,
    "axes.titlecolor": TEXT,
    "axes.titlesize": 10,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "xtick.color": TEXT2,
    "ytick.color": TEXT2,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "legend.frameon": False,
    "legend.fontsize": 8,
})


def to_png_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return b64encode(buf.getvalue()).decode()


def option_npv_chart(table: pd.DataFrame, recommended_label: str):
    """NPV per option, grouped by power rating, one bar per duration."""
    powers = sorted(table["power_mw"].unique())
    durations = sorted({float(o.split("/")[1].strip().rstrip(" h")) for o in table["option"]})
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    width = 0.8 / len(durations)
    colors = [S1, S2, S3, S4]
    for j, d in enumerate(durations):
        sub = table[table["option"].str.endswith(f"/ {d:g} h")].set_index("power_mw").reindex(powers)
        x = np.arange(len(powers)) + (j - (len(durations) - 1) / 2) * width
        bars = ax.bar(x, sub["npv_meur"], width * 0.92, color=colors[j % 4], label=f"{d:g} h",
                      edgecolor=SURFACE, linewidth=1.0)
        for xi, (lab, v) in zip(x, zip(sub["option"], sub["npv_meur"])):
            if lab == recommended_label:
                ax.annotate("recommended", (xi, v), xytext=(0, 6 if v >= 0 else -14),
                            textcoords="offset points", ha="center", fontsize=8, color=TEXT,
                            fontweight="bold")
    ax.axhline(0, color=TEXT2, linewidth=0.8)
    lo, hi = min(table["npv_meur"].min(), 0), max(table["npv_meur"].max(), 0)
    ax.set_ylim(lo * 1.15 if lo < 0 else 0, hi * 1.18 if hi > 0 else 0.1)
    ax.set_xticks(np.arange(len(powers)), [f"{p:g} MW" for p in powers])
    ax.set_ylabel("NPV of the BESS [M€]")
    ax.set_title("Net present value by BESS size (incremental to PV-only)")
    ax.legend(title="Duration", title_fontsize=8, ncols=len(durations), loc="upper left")
    ax.grid(axis="x", visible=False)
    return fig


def dispatch_week_chart(ts: pd.DataFrame, export_limit: float, start: pd.Timestamp, days: int = 4):
    """Power flows, price and state of charge for a few days (three panels)."""
    end = start + pd.Timedelta(days=days)
    w = ts.loc[start:end - pd.Timedelta(hours=1)].copy()
    w.index = w.index.tz_convert("Europe/Berlin")
    x = w.index

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(7.2, 6.2), sharex=True,
                                        gridspec_kw={"height_ratios": [3, 1.3, 1.2]})
    # positive = into the grid, negative = into the battery
    pos = [("pv_grid", w["pv_grid"]), ("dis", w["dis"])]
    neg = [("pv_bat", -w["pv_bat"]), ("grid_bat", -w["grid_bat"])]
    for parts in (pos, neg):
        bottom = np.zeros(len(w))
        for key, vals in parts:
            color, label = ROLE[key]
            ax1.fill_between(x, bottom, bottom + vals.to_numpy(), step="post", color=color,
                             label=label, linewidth=0, alpha=0.95)
            bottom = bottom + vals.to_numpy()
    ax1.fill_between(x, w["pv"] - w["curt"], w["pv"], step="post",
                     facecolor="none", edgecolor=NEUTRAL, hatch="////", linewidth=0, label="PV curtailed")
    ax1.step(x, w["pv"], where="post", color=TEXT, linewidth=1.0, label="PV available")
    ax1.axhline(export_limit, color=TEXT2, linestyle="--", linewidth=0.9)
    ax1.text(x[0], export_limit, " grid limit", va="bottom", fontsize=7.5, color=TEXT2)
    ax1.axhline(0, color=TEXT2, linewidth=0.6)
    ax1.set_ylabel("Power [MW]")
    ax1.set_title("Example dispatch (recommended option)")
    ax1.legend(ncols=3, loc="upper left", bbox_to_anchor=(0, -0.02), fontsize=7.5)

    ax2.step(x, w["price"], where="post", color=S1, linewidth=1.4)
    ax2.axhline(0, color=TEXT2, linewidth=0.6)
    ax2.fill_between(x, w["price"], 0, where=w["price"] < 0, step="post", color=S1, alpha=0.15)
    ax2.set_ylabel("Price\n[€/MWh]")

    ax3.step(x, w["soc"], where="post", color=S1, linewidth=1.4)
    ax3.set_ylabel("Energy in\nBESS [MWh]")
    ax3.set_ylim(bottom=0)
    import matplotlib.dates as mdates
    ax3.xaxis.set_major_locator(mdates.DayLocator(tz=x.tz))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%a %d.%m.", tz=x.tz))
    ax3.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18], tz=x.tz))
    fig.align_ylabels()
    fig.tight_layout(h_pad=2.6)
    return fig


def monthly_chart(ts: pd.DataFrame, base_ts: pd.DataFrame, dt: float):
    """Monthly extra revenue of the BESS compared to the PV-only plant."""
    rev = (ts["price"] * (ts["pv_grid"] + ts["dis"] - ts["grid_bat"])) * dt
    base = (base_ts["price"] * base_ts["pv_grid"]) * dt
    loc = ts.index.tz_convert("Europe/Berlin")
    extra = (rev - base).groupby(loc.month).sum() / 1e3
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    ax.bar([months[m - 1] for m in extra.index], extra.to_numpy(), color=S1, width=0.6)
    ax.set_ylabel("k€")
    ax.set_title("Extra revenue from the BESS per month (year 1)")
    ax.grid(axis="x", visible=False)
    return fig


def price_duration_chart(price: pd.Series, capture_price: float):
    p = np.sort(price.to_numpy())[::-1]
    hours = np.arange(1, len(p) + 1)
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    ax.plot(hours, p, color=S1, linewidth=1.6)
    ax.fill_between(hours, p, 0, where=p < 0, color=S1, alpha=0.18)
    ax.axhline(0, color=TEXT2, linewidth=0.6)
    ax.axhline(price.mean(), color=TEXT2, linestyle="--", linewidth=0.9)
    ax.text(len(p) * 0.02, price.mean(), f"average {price.mean():.0f} €/MWh", ha="left", va="bottom",
            fontsize=7.5, color=TEXT2)
    ax.axhline(capture_price, color=TEXT2, linestyle=":", linewidth=1.1)
    ax.text(len(p) * 0.02, capture_price, f"PV capture price {capture_price:.0f} €/MWh", ha="left",
            va="top", fontsize=7.5, color=TEXT2)
    neg = int((p < 0).sum())
    if neg:
        ax.text(len(p) - neg - len(p) * 0.01, -12, f"{neg} h with negative prices", ha="right",
                va="top", fontsize=7.5, color=TEXT)
    ax.set_xlim(0, len(p))
    lo, hi = np.percentile(p, [0.2, 99.8])
    ax.set_ylim(min(lo, -20) * 1.1, hi * 1.1)
    ax.set_xlabel("Hours of the year, sorted by price")
    ax.set_ylabel("€/MWh")
    ax.set_title("Day-ahead price duration curve")
    return fig


def pick_example_window(ts: pd.DataFrame, days: int = 4) -> pd.Timestamp:
    """Start of the window where the battery stores the most PV energy, so the
    chart shows both PV shifting and trading."""
    score = (ts["pv_bat"] + 0.2 * ts["dis"]).rolling(24 * days).sum()
    end = score.idxmax()
    start = (end - pd.Timedelta(hours=24 * days - 1)).tz_convert("Europe/Berlin").normalize()
    return start.tz_convert("UTC")
