"""Tourist pressure index: components, normalisation and a weighted composite.

    uv run python -m prisa.pressure

The definition isn't fixed, so the index is built from interchangeable
components. Each is computed per (day, barri) from `barri_day`, ranked
against the barri's own history (0-1) and stored long in
`pressure_components`; `composite` mixes them with any weights. Static things
(hotspots, bed capacity) are not components: they don't change from day to
day, so they can't tell a cheap day from an expensive one.

The same normalisation is applied to forecasts (`rank_against_history`), so a
2027 day gets the pressure it would have had in 2023-2025 terms.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

import numpy as np
import pandas as pd

from prisa import geo, schemas, store

COMPONENTS = {
    "visitor_density": "non-resident visitors present / registered residents",
    "inbound_intensity": "trips arriving in the barri per km2",
    "visitor_arrivals": "trips arriving in the barri made by non-residents",
    "anomaly": "trips arriving vs the barri's own median for that weekday and month",
    "transit_load": "mean peak-hour saturation ratio of the stops in the barri",
}

# Starting points to argue about, not answers. Weights are renormalised over
# the components present, so a preset works before transit data exists.
PRESETS: dict[str, dict[str, float]] = {
    "visitors": {"visitor_density": 0.5, "visitor_arrivals": 0.5},
    "transport": {"transit_load": 1.0},
    "balanced": {"visitor_density": 0.3, "visitor_arrivals": 0.2, "inbound_intensity": 0.2, "transit_load": 0.3},
}
DEFAULT_PRESET = "balanced"

# Display levels used by the team's calendar mockup, cut from the 0-1 index
TIERS = ("Muy baja", "Baja", "Media", "Alta", "Muy alta")
TIER_EDGES = (0.2, 0.4, 0.6, 0.8)


def tier(value: pd.Series) -> pd.Series:
    idx = np.searchsorted(TIER_EDGES, value.to_numpy(), side="right")
    return pd.Series(np.array(TIERS)[idx], index=value.index, dtype="string")


def _population_by_year(population: pd.DataFrame, zone_ids: pd.Series, years: pd.Series) -> np.ndarray:
    """Residents for each (zone, year), using the nearest published year when one is missing."""
    pop = population.pivot_table(index="zone_id", columns="year", values="population")
    pop = pop.reindex(columns=range(min(pop.columns.min(), years.min()), max(pop.columns.max(), years.max()) + 1))
    pop = pop.ffill(axis=1).bfill(axis=1)
    return pop.stack().reindex(pd.MultiIndex.from_arrays([zone_ids, years])).to_numpy()


def raw_components(barri_day: pd.DataFrame, zones: pd.DataFrame, population: pd.DataFrame) -> pd.DataFrame:
    """Long frame day, zone_id, component, raw for every component computable from `barri_day`."""
    d = barri_day.copy()
    d["day"] = pd.to_datetime(d["day"])
    area = d["zone_id"].map(zones.set_index("zone_id")["area_km2"])
    pop = _population_by_year(population, d["zone_id"], d["day"].dt.year)
    visitor_trips = d["trips_in"] - d["trips_in_resident"]
    key = [d["zone_id"], d["day"].dt.day_of_week, d["day"].dt.month]
    raw = {
        "visitor_density": d["visitors"] / pop,
        "inbound_intensity": d["trips_in"] / area,
        "visitor_arrivals": visitor_trips,
        "anomaly": d["trips_in"] / d.groupby(key)["trips_in"].transform("median"),
    }
    out = [
        pd.DataFrame({"day": d["day"], "zone_id": d["zone_id"], "component": name, "raw": vals.astype(float)})
        for name, vals in raw.items()
    ]
    return pd.concat(out, ignore_index=True).dropna(subset=["raw"])


def normalise(df: pd.DataFrame, scope: str = "zone") -> pd.Series:
    """Percentile rank of `raw` in 0-1.

    scope="zone": each barri against its own history, so every barri has its
    own quiet and busy days (what a daily discount needs).
    scope="global": all barris on one scale (what a map comparing barris needs).
    """
    by = ["component", "zone_id"] if scope == "zone" else ["component"]
    if scope not in ("zone", "global"):
        raise ValueError(f"unknown scope {scope!r}")
    return df.groupby(by)["raw"].rank(pct=True)


def components(raw: pd.DataFrame, scope: str = "zone") -> pd.DataFrame:
    unknown = set(raw["component"]) - set(COMPONENTS)
    if unknown:
        raise KeyError(f"unknown components {unknown}; add them to COMPONENTS first")
    out = raw.assign(normalised=normalise(raw, scope))
    out = out.astype({"zone_id": "string", "component": "string"})
    return out[schemas.PRESSURE_COMPONENTS.column_names]


def rank_against_history(history: pd.DataFrame, new: pd.DataFrame) -> pd.Series:
    """Percentile of each new (zone, component) raw value within that zone's historical raw values."""
    out = pd.Series(np.nan, index=new.index)
    for (comp, zone), h in history.groupby(["component", "zone_id"]):
        mask = (new["component"] == comp) & (new["zone_id"] == zone)
        if mask.any():
            ref = np.sort(h["raw"].to_numpy())
            out[mask] = np.searchsorted(ref, new.loc[mask, "raw"].to_numpy(), side="right") / len(ref)
    return out


def composite(components: pd.DataFrame, weights: Mapping[str, float], weights_id: str) -> pd.DataFrame:
    """Weighted mean of normalised components, skipping components a row lacks."""
    wide = components.pivot_table(index=["day", "zone_id"], columns="component", values="normalised")
    w = pd.Series({k: v for k, v in weights.items() if k in wide.columns}, dtype=float)
    if w.empty:
        raise ValueError(f"none of {list(weights)} are in the components table")
    present = wide[w.index].notna()
    value = (wide[w.index].fillna(0) * w).sum(axis=1) / (present * w).sum(axis=1)
    out = value.rename("value").reset_index()
    out["zone_id"] = out["zone_id"].astype("string")
    out["weights_id"] = pd.Series(weights_id, index=out.index, dtype="string")
    return out[schemas.PRESSURE_INDEX.column_names]


def city_pressure(index: pd.DataFrame, weights: pd.Series) -> pd.DataFrame:
    """One value per day: barri pressure averaged with weights (e.g. each barri's share of visitors).

    The fare is one price per day for the whole city, so it follows the
    barris where most visitors are: a quiet day in a residential barri
    matters less than a busy day in the Eixample.
    """
    w = index["zone_id"].map(weights).fillna(0)
    out = (index.assign(w=w, wv=w * index["value"]).groupby("day")[["wv", "w"]].sum())
    return (out["wv"] / out["w"]).rename("value").reset_index()


def transit_component(transit_load: pd.DataFrame, density: float = 4.0) -> pd.DataFrame:
    """The transit_load component: peak-hour load ratio at the planning standard (4 people/m2)."""
    t = transit_load[transit_load["standing_density"] == density]
    return pd.DataFrame({"day": pd.to_datetime(t["day"]), "zone_id": t["zone_id"], "component": "transit_load",
                         "raw": t["ratio_peak_hour"].astype(float)})  # fmt: skip


def build() -> dict[str, pd.DataFrame]:
    bd, zones, pop = store.read("barri_day"), store.read("zones"), store.read("zone_population")
    raw = raw_components(bd, zones, pop)
    if (store.PROCESSED / "transit_load.parquet").exists():
        raw = pd.concat([raw, transit_component(store.read("transit_load"))], ignore_index=True)
    comps = components(raw)
    store.write(comps, "pressure_components")
    present = set(comps["component"])
    usable = {name: w for name, w in PRESETS.items() if present & set(w)}  # skip presets with no data yet
    index = pd.concat([composite(comps, w, name) for name, w in usable.items()], ignore_index=True)
    store.write(index, "pressure_index")
    return {"pressure_components": comps, "pressure_index": index}


def visitor_weights(barri_day: pd.DataFrame) -> pd.Series:
    """Each barri's share of all visitor-days, the default weight for city pressure."""
    v = barri_day.groupby("zone_id")["visitors"].sum()
    return v / v.sum()


def main() -> int:
    out = build()
    for name, df in out.items():
        print(f"{name}: {len(df):,} rows")
    print("presets built:", sorted(out["pressure_index"]["weights_id"].unique()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
