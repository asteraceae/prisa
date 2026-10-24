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

    capacity_daily(stop, line) = departures x vehicle capacity at a standing density

    ratio_peak = ratio_daily x peak_demand_share / peak_service_share

Standing density: there is no single standard. European planning for buses,
trams and metros commonly uses 4 people/m2 for a full but acceptable vehicle,
and treats 6 people/m2 as crush load. We run both and report both, so a day
counts as saturated "at planning standard" or "even at crush load".
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
    # Standing densities (people/m2) to compute capacity at; one result set each.
    standing_densities: tuple[float, ...] = (4.0, 6.0)
    # Density assumed behind a published capacity when neither the standing
    # area nor the operator's basis is known.
    assumed_basis_ppm2: float = 4.0


def capacity_at(vehicle_types: pd.DataFrame, density: float, assumed_basis_ppm2: float = 4.0) -> pd.DataFrame:
    """Capacity of each vehicle type at `density` people/m2 standing.

    Uses the standing area when known; otherwise backs it out of the published
    capacity and its basis (or `assumed_basis_ppm2`), flagging those rows.
    """
    vt = vehicle_types.copy()
    seated = vt["capacity_seated"].astype(float).fillna(0.0)
    basis = vt["capacity_basis_ppm2"].astype(float).fillna(assumed_basis_ppm2)
    derived_area = (vt["capacity_total"].astype(float) - seated) / basis
    area = vt["standing_area_m2"].astype(float).fillna(derived_area)
    vt["capacity"] = seated + area * density
    vt["capacity_estimated"] = vt["standing_area_m2"].isna()
    return vt[["vehicle_type", "mode", "capacity", "capacity_estimated"]]


def line_capacity(lines: pd.DataFrame, vehicle_types: pd.DataFrame, density: float, a: Assumptions) -> pd.Series:
    cap = capacity_at(vehicle_types, density, a.assumed_basis_ppm2).set_index("vehicle_type")["capacity"]
    return lines.set_index("line_id")["vehicle_type"].map(cap)


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


def saturation_by_density(
    flows: pd.DataFrame,
    service: pd.DataFrame,
    stations: pd.DataFrame,
    lines: pd.DataFrame,
    vehicle_types: pd.DataFrame,
    a: Assumptions,
) -> pd.DataFrame:
    """`saturation` once per standing density in `a.standing_densities`, stacked."""
    runs = []
    for density in a.standing_densities:
        shares = stop_shares(service, stations, line_capacity(lines, vehicle_types, density, a))
        runs.append(saturation(flows, shares, a).assign(standing_density=density))
    return pd.concat(runs, ignore_index=True)
