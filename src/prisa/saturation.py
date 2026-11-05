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

import json
from dataclasses import dataclass, field

import numpy as np
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


def mode_share_from_table(table: pd.DataFrame) -> dict[Segment, dict[TransportMode, float]]:
    """Turn the `mode_share` reference table into `Assumptions.mode_share`.

    Only public transport modes are kept: walking, bikes, cars and taxis don't
    load the vehicles we measure, so they just reduce the riders.
    """
    pt = {m.value for m in TransportMode}
    out: dict[Segment, dict[TransportMode, float]] = {}
    for r in table[table["mode"].isin(pt)].itertuples():
        out.setdefault(Segment(r.segment), {})[TransportMode(r.mode)] = float(r.share)
    return out


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


# --- barri x day load index, for the pressure index ---------------------------


def day_types(calendar_days: pd.DataFrame, days: pd.Series) -> pd.Series:
    """weekday / saturday / sunday_holiday for each day, as in the GTFS service table."""
    cal = calendar_days.assign(day=pd.to_datetime(calendar_days["day"])).set_index("day")
    d = pd.to_datetime(days)
    hol = cal["public_holiday"].reindex(d).fillna(False).to_numpy(dtype=bool)
    dow = d.dt.day_of_week.to_numpy()
    out = np.where(hol | (dow == 6), "sunday_holiday", np.where(dow == 5, "saturday", "weekday"))
    return pd.Series(out, index=days.index)


def barri_capacity(service: pd.DataFrame, stations: pd.DataFrame, lines: pd.DataFrame,
                   vehicle_types: pd.DataFrame, density: float, a: Assumptions) -> pd.DataFrame:
    """Places offered per barri x mode x day type, and the share of departures in the busiest hour."""
    cap = capacity_at(vehicle_types, density, a.assumed_basis_ppm2).set_index("vehicle_type")["capacity"]
    s = service.merge(stations[["station_id", "zone_id"]], on="station_id").merge(
        lines[["line_id", "mode", "vehicle_type"]], on="line_id")
    s["places"] = s["departures"] * s["vehicle_type"].map(cap)
    s = s.dropna(subset=["places", "zone_id"])  # vehicle types without a capacity (funiculars) drop out
    g = s.groupby(["zone_id", "mode", "day_type"])
    return pd.DataFrame({
        "places": g["places"].sum(),
        "service_peak_share": g["departures_peak_hour"].sum() / g["departures"].sum(),
    }).reset_index()  # fmt: skip


def transit_load(barri_day: pd.DataFrame, calendar_days: pd.DataFrame, service: pd.DataFrame,
                 stations: pd.DataFrame, lines: pd.DataFrame, vehicle_types: pd.DataFrame,
                 peak_demand_share: dict[str, float], a: Assumptions, by_mode: bool = False) -> pd.DataFrame:
    """Rides starting in each barri / places offered there, per day and standing density (and mode if by_mode).

    Boardings per segment = trips leaving the barri, split by the segment mix
    of trips arriving there (Telefónica gives segments on arrivals), times
    each segment's share of each public-transport mode. It is a load *index*,
    not a measured occupancy: riders already on board from other barris only
    enter through `through_load`. That's enough to rank days, which is what
    the pressure index needs. `peak_demand_share` is the busiest hour's share
    of daily demand per day type.
    """
    bd = barri_day.assign(day=pd.to_datetime(barri_day["day"]))
    bd["day_type"] = day_types(calendar_days, bd["day"])
    seg_cols = {s: f"trips_in_{s.value}" for s in Segment}
    mix = bd[list(seg_cols.values())].div(bd["trips_in"].replace(0, np.nan), axis=0).fillna(0)
    rows = []
    for mode in {m for shares in a.mode_share.values() for m in shares}:
        rides = sum(bd["trips_out"] * mix[col] * a.mode_share.get(seg, {}).get(mode, 0) for seg, col in seg_cols.items())
        rows.append(bd[["day", "zone_id", "day_type"]].assign(mode=mode.value, demand=rides * a.legs_per_trip * a.through_load))
    demand = pd.concat(rows, ignore_index=True)

    out = []
    for density in a.standing_densities:
        cap = barri_capacity(service, stations, lines, vehicle_types, density, a)
        d = demand.merge(cap, on=["zone_id", "mode", "day_type"], how="inner")
        d["peak_demand_share"] = d["day_type"].map(peak_demand_share)
        agg = d.assign(peak_places=d["places"] * d["service_peak_share"],
                       peak_demand=d["demand"] * d["peak_demand_share"]).groupby(
            ["day", "zone_id"] + (["mode"] if by_mode else []))
        o = agg[["demand", "places", "peak_demand", "peak_places"]].sum().reset_index()
        o["ratio_daily"] = o["demand"] / o["places"]
        o["ratio_peak_hour"] = o["peak_demand"] / o["peak_places"]
        out.append(o.assign(standing_density=density))
    return pd.concat(out, ignore_index=True)


def peak_shares() -> dict[str, float]:
    """Busiest hour's share of daily demand per day type: MITMA hourly profile if built, else Renfe 2018."""
    from prisa import store

    for name, scope_filter in (("hourly_profile_mitma", None), ("hourly_profile_ridership", "rodalies_bcn_stations")):
        path = store.PROCESSED / f"{name}.parquet"
        if not path.exists():
            continue
        h = pd.read_parquet(path)
        if scope_filter is not None:
            h = h[h["scope"] == scope_filter]
        elif "district_code" in h:
            h = h[h["district_code"].isna()] if h["district_code"].isna().any() else h
        if "traveller" in h:
            h = h.groupby(["day_type", "hour"], as_index=False)["share"].mean()
        peak = h.groupby("day_type")["share"].max().to_dict()
        weekday = peak.get("weekday")
        return {t: peak.get(t, weekday) for t in ("weekday", "saturday", "sunday_holiday")}
    raise FileNotFoundError("no hourly profile built yet")


def calibrate_through_load(load_by_mode: pd.DataFrame, occupancy_hourly: pd.DataFrame,
                           calendar_days: pd.DataFrame) -> dict[str, float]:
    """Scale factor making the model's FGC weekday peak match FGC's measured peak occupancy.

    Measured: FGC Geotren (our own archive), mean over Barcelona lines of each
    line's busiest weekday hour, observation-weighted. Modelled: FGC peak-hour
    ratio on weekdays at 4 people/m2, places-weighted over the barris FGC
    serves. One factor for all modes is an assumption: FGC is the only
    operator whose real occupancy we can see.
    """
    occ = occupancy_hourly[occupancy_hourly["day_type"] == "weekday"]
    peak_by_line = occ.loc[occ.groupby("line_id")["mean_occupancy_pct"].idxmax()]
    observed = float((peak_by_line["mean_occupancy_pct"] * peak_by_line["observations"]).sum()
                     / peak_by_line["observations"].sum() / 100)  # fmt: skip
    f = load_by_mode[(load_by_mode["mode"] == "fgc") & (load_by_mode["standing_density"] == 4.0)]
    f = f[day_types(calendar_days, f["day"]).to_numpy() == "weekday"]
    modelled = float(f["peak_demand"].sum() / f["peak_places"].sum())
    return {"observed_fgc_peak": observed, "modelled_fgc_peak": modelled, "through_load": observed / modelled}


def build() -> pd.DataFrame:
    from prisa import reference, schemas, store

    a = Assumptions(mode_share=mode_share_from_table(reference.load(schemas.MODE_SHARE)))
    args = (store.read("barri_day"), store.read("calendar_days"), store.read("service_by_daytype"),
            store.read("stations"), store.read("lines"), reference.load(schemas.VEHICLE_TYPES), peak_shares())  # fmt: skip
    occ_path = store.PROCESSED / "occupancy_hourly.parquet"
    if occ_path.exists():
        cal = calibrate_through_load(transit_load(*args, a, by_mode=True), pd.read_parquet(occ_path), args[1])
        a.through_load = cal["through_load"]
        (store.PROCESSED / "transit_load_calibration.json").write_text(json.dumps(cal, indent=2))
        print("calibration:", {k: round(v, 4) for k, v in cal.items()})
    load = transit_load(*args, a)
    load["zone_id"] = load["zone_id"].astype("string")
    store.write(load, "transit_load")
    return load


if __name__ == "__main__":
    out = build()
    print(f"transit_load: {len(out):,} rows")
    print(out.groupby("standing_density")[["ratio_daily", "ratio_peak_hour"]].describe().round(3).T.to_string())
