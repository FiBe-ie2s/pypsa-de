# SPDX-License-Identifier: MIT
"""
Compare German electricity-price time series across dispatch variants of the
default4712SS fixed-capacity / copperplate operations runs, for 2025 and 2035:

  * Fixed-Cap (perfect-foresight single pass)          -> baseline
  * LUC        (single pass + linearized unit commitment)
  * RH 96h / RH 336h (rolling horizon, 4-day / 2-week window)

Two figures, each a 2 x 3 grid (rows: planning year; columns: metric):
  1. Price duration curve
  2. Daily price spread   (max - min within each calendar day)
  3. Weekly price spread  (max - min within each calendar week)

Metrics 2/3 quantify the arbitrage window a BESS could exploit at the
short-cycle (daily) and multi-day (weekly) horizon.

Outputs (next to this script):
  luc_vs_fixedcap.png, rh_vs_fixedcap.png
"""

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CSV = "results/{prefix}/KN2045_Mix/csvs/electricity_prices_s_27__none_{year}_op.csv"
OUT = Path(__file__).resolve().parent
YEARS = [2025, 2035]

# Okabe-Ito, colour-blind safe. Fixed-Cap is a deliberately neutral reference.
BASELINE = ("Fixed-Cap", "default4712SS_fixedcap", "#333333")
COMPARISONS = {
    "luc_vs_fixedcap": {
        "title": "Linearized Unit Commitment vs. Fixed-Capacity dispatch",
        "series": [BASELINE, ("LUC", "default4712SS_fixedcap_LUC", "#D55E00")],
    },
    "rh_vs_fixedcap": {
        "title": "Rolling Horizon vs. Fixed-Capacity dispatch",
        "series": [
            BASELINE,
            ("RH 96h", "default4712SS_fixedcap_rh96", "#0072B2"),
            ("RH 336h", "default4712SS_fixedcap_rh336", "#009E73"),
        ],
    },
}

# Rows whose comparison is not valid and must not be interpreted as an effect of
# the varied mechanism (annotated in the figure). Keyed by (figure, year).
CAVEATS = {
    ("rh_vs_fixedcap", 2025): (
        "⚠  Not comparable: RH used CO₂ price 123.6 €/t, but the correct "
        "2025 value is 0 (budget non-binding). Re-run with co2_price: 0 pending."
    ),
}

mpl.rcParams.update({
    "figure.dpi": 130,
    "savefig.dpi": 200,
    "font.size": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": "#e6e6e3",
    "grid.linewidth": 0.8,
    "axes.edgecolor": "#8a8a86",
    "text.color": "#222222",
    "axes.labelcolor": "#222222",
    "xtick.color": "#555555",
    "ytick.color": "#555555",
})


def de_price(prefix: str, year: int) -> pd.Series:
    df = pd.read_csv(ROOT / CSV.format(prefix=prefix, year=year),
                     index_col=0, parse_dates=True)
    col = "price_DE" if "price_DE" in df.columns else \
        next(c for c in df.columns if c.startswith("DE"))
    return df[col]


def make_figure(key: str, spec: dict) -> None:
    series = spec["series"]
    fig, axes = plt.subplots(len(YEARS), 3, figsize=(13.5, 7.4))
    metrics = ["Price duration curve", "Daily price spread", "Weekly price spread"]

    for r, year in enumerate(YEARS):
        prices = {name: de_price(prefix, year) for name, prefix, _ in series}

        # --- column 1: price duration curve ---
        ax = axes[r, 0]
        for (name, _, color) in series:
            s = prices[name].sort_values(ascending=False).to_numpy()
            x = 100.0 * (pd.RangeIndex(len(s)) + 0.5) / len(s)
            ax.plot(x, s, color=color, lw=2.0, label=name)
        ax.set_xlim(0, 100)
        ax.set_xlabel("Share of hours [%]")
        ax.set_ylabel("Price [€/MWh]")

        # --- columns 2 & 3: daily / weekly spread distributions ---
        for c, freq in [(1, "D"), (2, "W")]:
            ax = axes[r, c]
            data, colors = [], []
            for (name, _, color) in series:
                s = prices[name]
                spread = s.resample(freq).max() - s.resample(freq).min()
                data.append(spread.dropna().to_numpy())
                colors.append(color)
            bp = ax.boxplot(data, patch_artist=True, widths=0.6,
                            showfliers=False, medianprops=dict(color="#222222", lw=1.4),
                            whiskerprops=dict(color="#8a8a86"),
                            capprops=dict(color="#8a8a86"))
            for patch, color in zip(bp["boxes"], colors):
                patch.set_facecolor(color)
                patch.set_alpha(0.55)
                patch.set_edgecolor(color)
            # mean marker
            for i, arr in enumerate(data, start=1):
                ax.scatter(i, arr.mean(), marker="D", s=22, color="#222222", zorder=3)
            ax.set_xticks(range(1, len(series) + 1))
            ax.set_xticklabels([n for n, _, _ in series], rotation=0, fontsize=8)
            ax.margins(x=0.12)
            ax.set_ylabel("Spread [€/MWh]")

        # invalid-comparison caveat banner across the row
        caveat = CAVEATS.get((key, year))
        if caveat:
            axes[r, 1].annotate(
                caveat, xy=(0.5, 1.12), xycoords="axes fraction",
                ha="center", va="bottom", fontsize=8.5, color="#B00020",
                bbox=dict(boxstyle="round,pad=0.4", fc="#fdecee", ec="#B00020", lw=1),
            )

    for c, m in enumerate(metrics):
        axes[0, c].set_title(m, fontsize=11, fontweight="bold", pad=8)

    handles = [plt.Line2D([0], [0], color=col, lw=2.4) for _, _, col in series]
    fig.legend(handles, [n for n, _, _ in series], loc="upper center",
               ncol=len(series), frameon=False, bbox_to_anchor=(0.5, 0.99),
               fontsize=10)
    fig.suptitle(spec["title"], y=1.045, fontsize=13, fontweight="bold")
    fig.text(0.5, -0.02,
             "Germany, single bidding zone (copperplate). Boxes: IQR, line: median, "
             "diamond: mean; whiskers 1.5·IQR, outliers hidden.",
             ha="center", fontsize=8, color="#666666")
    fig.tight_layout(rect=[0.05, 0.0, 1, 0.96], w_pad=2.0)
    # year labels placed after layout, left of the y-axis titles (no collision)
    for r, year in enumerate(YEARS):
        pos = axes[r, 0].get_position()
        fig.text(0.012, (pos.y0 + pos.y1) / 2, str(year), rotation=90,
                 va="center", ha="center", fontsize=13, fontweight="bold",
                 color="#222222")
    fig.savefig(OUT / f"{key}.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {OUT / (key + '.png')}")


if __name__ == "__main__":
    for key, spec in COMPARISONS.items():
        make_figure(key, spec)
