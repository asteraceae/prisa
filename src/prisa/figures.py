"""The project's charts, written to outputs/figures/ (git-ignored: they show confidential data).

    uv run python -m prisa.figures

Each function takes what it plots and returns the matplotlib Figure, so the
notebooks call the same code and show the same pictures.
"""

from __future__ import annotations

import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from shapely import wkt  # noqa: E402

from prisa import forecast, geo, pressure, pricing, store  # noqa: E402

FIG_DIR = store.OUTPUTS / "figures"
SEGMENT_COLOURS = {"international": "#1f5c7a", "national": "#7fa7bf", "regional": "#c79a1e"}
TIER_COLOURS = {"Muy baja": "#fbf1e3", "Baja": "#f7d9ae", "Media": "#f0b26b", "Alta": "#db7a3a", "Muy alta": "#9e3a1a"}

plt.rcParams.update({
    "figure.dpi": 110, "savefig.dpi": 150, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.25, "font.size": 9,
})  # fmt: skip


def city_visitors(city_day: pd.DataFrame, forecasts: pd.DataFrame | None = None) -> plt.Figure:
    """Daily non-resident visitors by segment (7-day mean), with the 2027 forecast if given."""
    c = city_day.set_index(pd.to_datetime(city_day["day"]))
    fig, ax = plt.subplots(figsize=(10, 3.6))
    bottom = np.zeros(len(c))
    for seg, colour in SEGMENT_COLOURS.items():
        v = c[f"visitors_{seg}"].rolling(7, center=True, min_periods=1).mean().to_numpy()
        ax.fill_between(c.index, bottom / 1e3, (bottom + v) / 1e3, color=colour, alpha=0.85, lw=0, label=seg)
        bottom += v
    if forecasts is not None:
        f = forecasts.set_index("day")
        ax.plot(f.index, f["predicted"].rolling(7, center=True, min_periods=1).mean() / 1e3, color="black", lw=1,
                label="2027 forecast (level held at 2025)")  # fmt: skip
        ax.fill_between(f.index, f["lower"] / 1e3, f["upper"] / 1e3, color="black", alpha=0.08, lw=0)
    ax.set_ylabel("visitors present (thousands)")
    ax.set_title("Non-resident visitors in Barcelona per day (7-day mean)")
    ax.legend(loc="upper left", ncols=4, frameon=False)
    return fig


def holdout(ev: dict, title: str) -> plt.Figure:
    """Forecast vs actual on the unseen Jan-Aug 2025 holdout (city series)."""
    t = ev["test_frame"].sort_values("day")
    fig, ax = plt.subplots(figsize=(10, 3.2))
    ax.plot(t["day"], t["y"] / 1e3, color="black", lw=1, label="actual")
    ax.plot(t["day"], t["pred"] / 1e3, color="#1f5c7a", lw=1.2, label=f"model (rank corr {ev['model']['spearman']:.2f})")
    ax.plot(t["day"], t["pred_baseline"] / 1e3, color="#c79a1e", lw=1, ls="--",
            label=f"weekday x month average (rank corr {ev['baseline']['spearman']:.2f})")  # fmt: skip
    ax.set_ylabel("thousands")
    ax.set_title(title)
    ax.legend(loc="lower right", frameon=False)
    return fig


def barri_map(values: pd.Series, geometry: pd.DataFrame, title: str, cmap: str = "YlOrBr", label: str = "",
              center: float | None = None) -> plt.Figure:
    """Choropleth of one value per barri (index = zone_id); `center` gives a diverging scale around that value."""
    fig, ax = plt.subplots(figsize=(6.5, 6))
    vmin, vmax = np.nanpercentile(values, 2), np.nanpercentile(values, 98)
    if center is None:
        norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax)
    else:
        span = max(center - vmin, vmax - center)
        norm = matplotlib.colors.TwoSlopeNorm(vcenter=center, vmin=center - span, vmax=center + span)
    cm = plt.get_cmap(cmap)
    for r in geometry.itertuples():
        shape = wkt.loads(r.wkt_wgs84)
        polys = shape.geoms if hasattr(shape, "geoms") else [shape]
        v = values.get(r.zone_id, np.nan)
        for poly in polys:
            x, y = poly.exterior.xy
            ax.fill(x, y, color=cm(norm(v)) if pd.notna(v) else "#dddddd", ec="white", lw=0.4)
    ax.set_aspect(1 / np.cos(np.radians(41.39)))
    ax.axis("off")
    ax.set_title(title)
    fig.colorbar(matplotlib.cm.ScalarMappable(norm=norm, cmap=cm), ax=ax, shrink=0.6, label=label)
    return fig


def calendar_heatmap(daily_fares: pd.DataFrame, title: str) -> plt.Figure:
    """One row per month, one cell per day, coloured by level, labelled with the discount."""
    d = daily_fares.assign(day=pd.to_datetime(daily_fares["day"]))
    tiers = list(TIER_COLOURS)
    fig, ax = plt.subplots(figsize=(11, 4.2))
    for r in d.itertuples():
        m, dom = r.day.month, r.day.day
        ax.add_patch(plt.Rectangle((dom - 0.5, 12.5 - m), 1, 1, color=TIER_COLOURS[r.tier], ec="white", lw=0.6))
        if r.discount > 0.005:
            ax.text(dom, 13 - m, f"{r.discount:.0%}", ha="center", va="center", fontsize=5.5,
                    color="white" if r.tier in ("Alta", "Muy alta") else "#3d1d08")  # fmt: skip
        if r.day.day_of_week >= 5:
            ax.plot([dom - 0.45, dom + 0.45], [12.55 - m, 12.55 - m], color="#14213d", lw=0.6)
    ax.set_xlim(0.5, 31.5)
    ax.set_ylim(0.5, 12.5)
    ax.set_yticks(range(12, 0, -1), ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    ax.set_xticks(range(1, 32))
    ax.tick_params(length=0, labelsize=7)
    ax.grid(False)
    handles = [plt.Rectangle((0, 0), 1, 1, color=TIER_COLOURS[t]) for t in tiers]
    ax.legend(handles, [f"{t}" for t in tiers], ncols=5, loc="upper center", bbox_to_anchor=(0.5, -0.06), frameon=False)
    ax.set_title(title + "  (cell text = discount; underline = weekend)")
    return fig


def simulation_tradeoff(summary: pd.DataFrame) -> plt.Figure:
    """Effect on the busiest days against revenue cost, per calendar design and scenario."""
    s = summary[summary["calendar_id"].str.startswith("prisa-2027")].copy()
    s["design"] = s["calendar_id"].str.replace("prisa-2027-", "")
    fig, ax = plt.subplots(figsize=(6.5, 4))
    markers = {"A": "o", "B": "s", "C": "^"}
    colours = {"year": "#7fa7bf", "month": "#1f5c7a", "hotspots": "#c79a1e"}
    for r in s.itertuples():
        ax.scatter(-r.revenue_delta_eur / 1e6, -100 * r.busiest_fifth_change, marker=markers[r.scenario_id],
                   color=colours[r.design], s=55, zorder=3)  # fmt: skip
    for design, colour in colours.items():
        g = s[s["design"] == design].sort_values("scenario_id")
        ax.plot(-g["revenue_delta_eur"] / 1e6, -100 * g["busiest_fifth_change"], color=colour, lw=1, label=design)
    for sc, m in markers.items():
        ax.scatter([], [], marker=m, color="grey", label=f"scenario {sc}")
    ax.set_xlabel("revenue given up per year (EUR million, visitor pay-per-trip rides)")
    ax.set_ylabel("busiest fifth of days: rides removed (%)")
    ax.set_title("What each calendar design buys, 2027")
    ax.legend(frameon=False, fontsize=8)
    return fig


def save(fig: plt.Figure, name: str) -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG_DIR / f"{name}.png", bbox_inches="tight")
    plt.close(fig)


def build() -> list[str]:
    city = store.read("city_day")
    fc, runs = store.read("forecasts"), store.read("model_runs")
    run = runs[runs["target"] == "city_visitors"].sort_values("created_at")["run_id"].iloc[-1]
    f27 = fc[(fc["run_id"] == run) & (fc["zone_id"] == geo.BARCELONA)]
    save(city_visitors(city, f27), "01_city_visitors")
    save(holdout(forecast.evaluate("city_visitors"), "City visitors, Jan-Aug 2025 (not seen in training)"), "02_holdout_city_visitors")

    geom = store.read("barri_geometry")
    bd = store.read("barri_day")
    # per km2, not per resident: barris with almost no residents (the port, Zona Franca) blow up a per-resident map
    area = store.read("zones").set_index("zone_id")["area_km2"]
    dens = bd.groupby("zone_id")["visitors"].mean() / area.reindex(bd["zone_id"].unique())
    save(barri_map(dens / 1e3, geom, "Non-resident visitors present per km2 (mean day, 2023-2025)",
                   label="thousand visitors / km2"), "03_map_visitor_density")  # fmt: skip
    summer = bd[pd.to_datetime(bd["day"]).dt.month.isin([7, 8])].groupby("zone_id")["visitors"].mean()
    winter = bd[pd.to_datetime(bd["day"]).dt.month.isin([1, 2])].groupby("zone_id")["visitors"].mean()
    save(barri_map(summer / winter, geom, "Summer vs winter visitors (Jul-Aug / Jan-Feb)", cmap="RdBu_r",
                   label="ratio (1 = no change)", center=1.0), "04_map_summer_winter")  # fmt: skip

    fares_ = store.read("daily_fares")
    for design in pricing.DESIGNS:
        cid = pricing.calendar_ids(design)[0]
        save(calendar_heatmap(fares_[fares_["calendar_id"] == cid], f"PRISA fare calendar 2027 - '{design}' design"),
             f"05_calendar_2027_{design}")  # fmt: skip
    save(simulation_tradeoff(store.read("simulation_summary")), "06_simulation_tradeoff")
    return sorted(p.name for p in FIG_DIR.glob("*.png"))


def main() -> int:
    for name in build():
        print(FIG_DIR / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
