"""Tourist pressure index: components, normalisation and a weighted composite.

The definition is not agreed yet, so the index is built from interchangeable
components. Each component is computed per (day, zone), normalised to 0-1 and
stored long in `pressure_components`; `composite` mixes them with any weights.
Static things (hotspots, bed capacity) are deliberately not components: they
don't change from day to day, so they can't tell a cheap day from an
expensive one. They enter the forecast model as features instead.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from prisa import schemas
from prisa.enums import Segment

COMPONENTS = {
    "visitor_density": "non-resident unique visitors present / registered residents",
    "inbound_intensity": "trips arriving in the zone (all segments) per km2",
    "transit_load": "mean peak-hour saturation ratio of the stops in the zone",
    "anomaly": "inbound trips relative to the zone's own median for that weekday and month",
}

# Starting points to argue about, not answers. Weights are renormalised over
# the components that are present, so a preset still works before transit
# data exists.
PRESETS: dict[str, dict[str, float]] = {
    "visitors": {"visitor_density": 0.6, "inbound_intensity": 0.4},
    "transport": {"transit_load": 1.0},
    "balanced": {"visitor_density": 0.3, "inbound_intensity": 0.2, "transit_load": 0.4, "anomaly": 0.1},
}


def inbound_trips(flows: pd.DataFrame) -> pd.DataFrame:
    return (
        flows.groupby(["day", "dest_zone_id"], as_index=False)["trips"]
        .sum()
        .rename(columns={"dest_zone_id": "zone_id"})
    )


def inbound_intensity(flows: pd.DataFrame, zones: pd.DataFrame) -> pd.DataFrame:
    df = inbound_trips(flows).merge(zones[["zone_id", "area_km2"]], on="zone_id")
    return df.assign(raw=df["trips"] / df["area_km2"])[["day", "zone_id", "raw"]]


def visitor_density(presence_daily: pd.DataFrame, population: pd.DataFrame) -> pd.DataFrame:
    visitors = (
        presence_daily[presence_daily["segment"] != Segment.RESIDENT.value]
        .groupby(["day", "zone_id"], as_index=False)["visitors"]
        .sum()  # segments and visitor types are disjoint groups; summing days is not allowed
    )
    visitors["year"] = visitors["day"].dt.year
    df = visitors.merge(population, on=["zone_id", "year"])
    return df.assign(raw=df["visitors"] / df["population"])[["day", "zone_id", "raw"]]


def anomaly(flows: pd.DataFrame) -> pd.DataFrame:
    df = inbound_trips(flows)
    key = [df["zone_id"], df["day"].dt.dayofweek, df["day"].dt.month]
    baseline = df.groupby(key)["trips"].transform("median")
    return df.assign(raw=df["trips"] / baseline)[["day", "zone_id", "raw"]]


def normalise(df: pd.DataFrame, scope: str = "zone") -> pd.Series:
    """Percentile rank of `raw` in 0-1.

    scope="zone": each zone against its own history, so every barri has its
    own quiet and busy days (what a daily discount needs).
    scope="global": all zones on one scale (what a map comparing barris needs).
    """
    if scope == "zone":
        return df.groupby("zone_id")["raw"].rank(pct=True)
    if scope == "global":
        return df["raw"].rank(pct=True)
    raise ValueError(f"unknown scope {scope!r}")


def component(name: str, raw: pd.DataFrame, scope: str = "zone") -> pd.DataFrame:
    if name not in COMPONENTS:
        raise KeyError(f"unknown component {name!r}; add it to COMPONENTS first")
    out = raw.assign(
        component=pd.Series(name, index=raw.index, dtype="string"),
        normalised=normalise(raw, scope),
    )
    out["zone_id"] = out["zone_id"].astype("string")
    return out[schemas.PRESSURE_COMPONENTS.column_names]


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
