"""How full the vehicles are: people passing / (vehicles x capacity), every mode.

Telefónica gives people moving between zones per day, with no mode and no
hour. Turning that into a load factor per stop needs several terms the raw
ratio hides. Each one is an explicit, documented assumption below so it can be
calibrated against operator ridership or varied in a sensitivity run.

    demand_daily(stop, line) =
        zone_boardings(zone of stop, day)          from trip_flows, by segment
      x expansion                                  Telefónica share -> population
      x mode_share(segment, mode)                  most short trips are walked
      x stop_share(stop, line)                     split the zone across its stops
      x legs_per_trip                              transfers board a 2nd vehicle
      x through_load                               people already on board

    capacity_daily(stop, line) = departures x vehicle capacity_total

    ratio_peak = ratio_daily x peak_demand_share / peak_service_share
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from prisa.enums import Segment, TransportMode


@dataclass
class Assumptions:
    # Telefónica counts its own customers; set >1 only if the published
    # figures are not already expanded to the whole population.
    expansion: float = 1.0
    # Value assumed for a censored (<15, published as 0) count. 0 is a lower
    # bound; 7 is the midpoint of 0-14.
    censored_fill: float = 0.0
    # Share of each segment's trips made on each mode. Placeholder: to be
    # replaced from EMEF / tourist surveys and calibrated on ridership.
    mode_share: dict[Segment, dict[TransportMode, float]] = field(default_factory=dict)
    # Average vehicles boarded per public transport trip (>= 1).
    legs_per_trip: float = 1.0
    # Multiplier for passengers already on board when the vehicle reaches the
    # stop (>= 1). Calibrate per line from ridership.
    through_load: float = 1.0
    # Share of a day's demand in its busiest hour. From hourly OD data where
    # available, else ridership profiles.
    peak_demand_share: float | None = None


def zone_boardings(flows: pd.DataFrame, a: Assumptions) -> pd.DataFrame:
    """Trips starting in each zone per day and segment (intra-zone trips are not observed)."""
    trips = flows["trips"].astype(float).where(~flows["censored"], a.censored_fill)
    return (
        flows.assign(trips=trips * a.expansion)
        .groupby(["day", "origin_zone_id", "segment"], as_index=False)["trips"]
        .sum()
        .rename(columns={"origin_zone_id": "zone_id"})
    )


def stop_shares(service: pd.DataFrame, stations: pd.DataFrame, vehicle_capacity: pd.Series) -> pd.DataFrame:
    """Share of a zone's boardings on each mode that each (stop, line, direction) gets.

    Split in proportion to the capacity offered there that day: a stop with
    more departures of bigger vehicles takes more of the zone's riders.
    """
    df = service.merge(stations[["station_id", "zone_id", "mode"]], on="station_id")
    df["capacity_daily"] = df["departures"] * df["line_id"].map(vehicle_capacity)
    df["stop_share"] = df["capacity_daily"] / df.groupby(["day", "zone_id", "mode"])["capacity_daily"].transform(
        "sum"
    )
    return df


def saturation(flows: pd.DataFrame, shares: pd.DataFrame, a: Assumptions) -> pd.DataFrame:
    boardings = zone_boardings(flows, a)
    by_mode = []
    for seg, modes in a.mode_share.items():
        for mode, share in modes.items():
            b = boardings[boardings["segment"] == seg.value]
            by_mode.append(b.assign(mode=mode.value, trips=b["trips"] * share))
    if not by_mode:
        raise ValueError("Assumptions.mode_share is empty: no trips are assigned to any mode")
    demand = pd.concat(by_mode).groupby(["day", "zone_id", "mode"], as_index=False)["trips"].sum()

    df = shares.merge(demand, on=["day", "zone_id", "mode"], how="left").fillna({"trips": 0.0})
    df["demand_daily"] = df["trips"] * df["stop_share"] * a.legs_per_trip * a.through_load
    df["ratio_daily"] = df["demand_daily"] / df["capacity_daily"]
    if a.peak_demand_share is not None and "departures_peak_hour" in df:
        peak_service_share = df["departures_peak_hour"] / df["departures"]
        df["ratio_peak_hour"] = df["ratio_daily"] * a.peak_demand_share / peak_service_share
    else:
        df["ratio_peak_hour"] = float("nan")
    return df[
        ["day", "station_id", "line_id", "direction", "mode",
         "demand_daily", "capacity_daily", "ratio_daily", "ratio_peak_hour"]
    ]  # fmt: skip
