"""Public transport supply in Barcelona: stops, lines, stop sequences, departures.

    uv run python -m prisa.external.transit [--refresh] [--only tmb fgc ...]
        [--today 2026-11-05] [--service-daily 2023-01-01 2025-08-31]

Downloads the GTFS feeds below into data/raw/external/transit/<operator>/
(skipped when already on disk; --refresh fetches again), keeps the stops that
fall inside the municipality of Barcelona and writes to data/processed/:

  stations            one row per stop/platform served (STATIONS contract)
  lines               one row per GTFS route serving Barcelona (LINES)
  line_stops          the most frequent stop pattern of each line and direction (LINE_STOPS)
  service_by_daytype  departures per line, stop, direction and day type (SERVICE_BY_DAYTYPE below)
  service_daily       only with --service-daily: service_by_daytype expanded over
                      calendar_days (SERVICE_DAILY)

Sources (downloaded 2026-11-05; each download also leaves a <zip>.json with
URL, time and Last-Modified next to the file):
  tmb    TMB metro, city bus and Montjuïc funicular. TMB's own GTFS needs an
         app_id/app_key from developer.tmb.cat. We use the Mobility Database
         public mirror of that same feed (MobilityData, catalog id mdb-2359),
         https://storage.googleapis.com/storage/v1/b/mdb-latest/o/es-barcelona-transports-metropolitans-de-barcelona-tmb-gtfs-2359.zip?alt=media
         Snapshot published by TMB 2026-04-10, valid 2026-04-10 to 2026-12-05.
         Licence: TMB developer terms (https://developer.tmb.cat), not an open
         licence; the mirror does not change that. Fine for this academic
         analysis, but do not redistribute the raw feed.
  fgc    FGC, https://www.fgc.cat/google/google_transit.zip. CC BY 4.0
         (dadesobertes.fgc.cat). Valid 2026-10-31 to 2026-12-31.
  renfe  Renfe Cercanías/Rodalies (all of Spain; we keep Barcelona),
         https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip.
         CC BY 4.0, attribution to Renfe (data.renfe.com/dataset/horarios-cercanias).
         Day-by-day calendar, 2026-11-05 to 2026-12-04.
  tram   TRAM Trambaix (TBX) and Trambesòs (TBS), https://opendata.tram.cat/GTFS/zip/TBX.zip
         and TBS.zip. Licence NOT verified: the portal shows only generic
         "terms and conditions of use" (no named licence). About 30 days of
         calendar from the download date.
  amb    AMB metropolitan buses (Nitbus, Aerobús, L/M/X lines; no TMB lines),
         https://www.ambmobilitat.cat/OpenData/google_transit.zip. Licence not
         stated for the static file; AMB's GTFS-RT dataset in the same catalogue
         is "Autorització segons Llei 37/2007" (free reuse with attribution and
         date of last update), very likely the same for the static feed.
Not covered: Telefèric de Montjuïc (cable car) and Funicular del Tibidabo
have no GTFS. Renfe replacement coaches (route_type 3) are kept as mode
rodalies with a bus vehicle: in Nov 2026 the R3 runs by coach from Fabra i
Puig, so its rail service is missing from this timetable.

Method and assumptions:
  * Barcelona = stops inside the barri polygons of data/processed/barri_geometry.parquet
    (zone_id = the barri). Without that file only the bounding box is applied
    and zone_id stays null (includes bits of L'Hospitalet, Esplugues, Sant Adrià...).
  * Representative week: per feed, the first Monday-Sunday week from the week
    of `today` on that has service every day and no public holiday Monday to
    Saturday (Catalan holidays + La Mercè + Whit Monday). weekday = mean of
    Monday-Friday, saturday = that Saturday, sunday_holiday = that Sunday.
    Means are rounded to whole departures.
  * Each scheduled call of a vehicle at a stop is one departure, including the
    terminus. Times past 24:00 belong to the service day they start in and go
    into hour (h mod 24). frequencies.txt trips are expanded at their headway.
  * Feeds without direction_id (FGC, TRAM, Renfe) get one from geometry: 0 if
    the trip runs the same way as the route's longest trip (first to last stop
    in Barcelona), else 1. Renfe already splits directions into route_ids, so
    its direction is nearly always 0.
  * line_stops keeps the GTFS stop_sequence of the most frequent pattern (Barcelona
    stops only, so numbers can skip). Stops served only by other variants
    of a line appear in service_by_daytype but not in line_stops.
  * expand_service_daily applies the current timetable to every date asked
    for, including past years (2023-2025): service changes since then are ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import holidays
import numpy as np
import pandas as pd
import shapely
from dateutil.easter import easter  # ships with pandas

from prisa import reference, schemas
from prisa.enums import TransportMode

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / "external" / "transit"
PROCESSED_DIR = ROOT / "data" / "processed"
GEOMETRY_PATH = PROCESSED_DIR / "barri_geometry.parquet"
CALENDAR_PATH = PROCESSED_DIR / "calendar_days.parquet"

# Barcelona municipality plus a small margin (lat_min, lat_max, lon_min, lon_max)
BBOX = (41.312, 41.475, 2.047, 2.234)


class DayType(StrEnum):
    WEEKDAY = "weekday"
    SATURDAY = "saturday"
    SUNDAY_HOLIDAY = "sunday_holiday"


SERVICE_BY_DAYTYPE = schemas.Table(
    "service_by_daytype",
    "dimension",
    ("line_id", "station_id", "direction", "day_type"),
    (
        schemas.C("line_id", schemas.STR),
        schemas.C("station_id", schemas.STR),
        schemas.C("direction", schemas.INT, "GTFS direction_id, or derived (see prisa.external.transit)"),
        schemas.C("day_type", schemas.STR, "weekday / saturday / sunday_holiday", enum=DayType),
        schemas.C("departures", schemas.INT, "scheduled departures on a typical day of this type"),
        schemas.C("departures_peak_hour", schemas.INT, "departures in the busiest clock hour"),
        schemas.C("peak_hour", schemas.INT, "0-23, clock hour with most departures (ties: earliest from 04:00)"),
    ),
    "Compact form of service_daily: one representative week of the current GTFS "
    "timetables. Expand to dates with expand_service_daily.",
    sources=("GTFS feeds",),
)

# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    key: str  # unique per zip
    operator: str  # id prefix and raw sub-directory
    url: str
    filename: str
    licence: str


FEEDS: tuple[Feed, ...] = (
    Feed(
        "tmb", "tmb",
        "https://storage.googleapis.com/storage/v1/b/mdb-latest/o/"
        "es-barcelona-transports-metropolitans-de-barcelona-tmb-gtfs-2359.zip?alt=media",
        "gtfs.zip", "TMB developer terms (via Mobility Database mirror mdb-2359)",
    ),
    Feed("fgc", "fgc", "https://www.fgc.cat/google/google_transit.zip", "gtfs.zip", "CC BY 4.0"),
    Feed(
        "renfe", "renfe", "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip",
        "gtfs.zip", "CC BY 4.0 (Renfe)",
    ),
    Feed("tram_tbx", "tram", "https://opendata.tram.cat/GTFS/zip/TBX.zip", "TBX.zip", "unverified (TRAM terms of use)"),
    Feed("tram_tbs", "tram", "https://opendata.tram.cat/GTFS/zip/TBS.zip", "TBS.zip", "unverified (TRAM terms of use)"),
    Feed(
        "amb", "amb", "https://www.ambmobilitat.cat/OpenData/google_transit.zip", "gtfs.zip",
        "probably AMB 'Llei 37/2007' free reuse (stated for its GTFS-RT dataset)",
    ),
)  # fmt: skip

# GTFS route_type -> mode, per operator (None = drop the route)
_ROUTE_MODES: dict[str, dict[str, TransportMode]] = {
    "tmb": {"1": TransportMode.METRO, "3": TransportMode.BUS, "7": TransportMode.FUNICULAR},
    "fgc": {"1": TransportMode.FGC, "2": TransportMode.FGC, "7": TransportMode.FUNICULAR, "3": TransportMode.BUS},
    # route_type 3 = replacement coaches during works (R3 north of Fabra i Puig in Nov 2026)
    "renfe": {"2": TransportMode.RODALIES, "3": TransportMode.RODALIES},
    "amb": {"3": TransportMode.BUS},
}

# Small automatic trains of L9/L10 (series 9000)
_METRO_9000 = {"L9", "L9N", "L9S", "L10", "L10N", "L10S"}

# Priority when one stop is served by several modes (it takes the first)
_MODE_PRIORITY = [
    TransportMode.METRO, TransportMode.RODALIES, TransportMode.FGC, TransportMode.TRAM,
    TransportMode.FUNICULAR, TransportMode.CABLE_CAR, TransportMode.BUS,
]  # fmt: skip


def route_mode(operator: str, route_type: str) -> TransportMode | None:
    if operator == "tram":  # T2 is published as route_type 2, still a tram
        return TransportMode.TRAM
    return _ROUTE_MODES.get(operator, {}).get(str(route_type).strip())


def vehicle_type(operator: str, mode: TransportMode, code: str, route_type: str = "") -> str:
    """Default vehicle of a line; must exist in data/reference/vehicle_types.csv to get a capacity."""
    if str(route_type).strip() == "3":  # any operator's bus, incl. Renfe replacement coaches
        return "bus_tmb_avg"
    if mode == TransportMode.METRO:
        return "metro_9000" if code in _METRO_9000 else "metro_tmb_avg"
    if mode == TransportMode.BUS:
        return "bus_tmb_avg"
    if mode == TransportMode.TRAM:
        return "tram_citadis_302"
    if mode == TransportMode.FGC:
        return "fgc_bv_unit"
    if mode == TransportMode.RODALIES:
        return "rodalies_civia_463"
    if mode == TransportMode.FUNICULAR:
        return {"tmb": "funicular_montjuic", "fgc": "funicular_vallvidrera"}.get(operator, "funicular")
    if mode == TransportMode.CABLE_CAR:
        return "cable_car_montjuic"
    raise ValueError(f"no vehicle type for {operator} {mode}")


# --------------------------------------------------------------------------
# Download and extract
# --------------------------------------------------------------------------

_MEMBERS = (
    "agency.txt", "feed_info.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt",
    "calendar.txt", "calendar_dates.txt", "frequencies.txt",
)  # fmt: skip


def download(feed: Feed, raw_dir: Path = RAW_DIR, refresh: bool = False) -> Path:
    """Fetch the feed's zip unless it is on disk already; returns its path."""
    path = raw_dir / feed.operator / feed.filename
    if path.exists() and not refresh:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {feed.key} from {feed.url}", file=sys.stderr)
    req = urllib.request.Request(feed.url, headers={"User-Agent": "prisa (UPF Mobile Data Challenge)"})
    tmp = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
        last_modified = r.headers.get("Last-Modified")
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink()
        raise ValueError(f"{feed.key}: {feed.url} did not return a zip file")
    tmp.rename(path)
    meta = {
        "feed": feed.key,
        "url": feed.url,
        "licence": feed.licence,
        "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "last_modified": last_modified,
        "bytes": path.stat().st_size,
    }
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    return path


def extract(zip_path: Path) -> Path:
    """Unzip the GTFS files we read next to the zip, once per zip version."""
    target = zip_path.parent / zip_path.stem
    stamp = f"{zip_path.stat().st_size} {zip_path.stat().st_mtime_ns}"
    marker = target / ".extracted"
    if marker.exists() and marker.read_text() == stamp:
        return target
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for name in _MEMBERS:  # fixed names only, so no path traversal
            if name in z.namelist():
                (target / name).write_bytes(z.read(name))
            elif (target / name).exists():
                (target / name).unlink()
    marker.write_text(stamp)
    return target


# --------------------------------------------------------------------------
# Read GTFS
# --------------------------------------------------------------------------


@dataclass
class Gtfs:
    stops: pd.DataFrame
    routes: pd.DataFrame
    trips: pd.DataFrame
    stop_times: pd.DataFrame
    calendar: pd.DataFrame
    calendar_dates: pd.DataFrame
    frequencies: pd.DataFrame


_COLUMNS = {
    "stops": ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type"],
    "routes": ["route_id", "route_short_name", "route_long_name", "route_type"],
    "trips": ["route_id", "service_id", "trip_id", "direction_id"],
    "stop_times": ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    "calendar": ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                 "start_date", "end_date"],
    "calendar_dates": ["service_id", "date", "exception_type"],
    "frequencies": ["trip_id", "start_time", "end_time", "headway_secs"],
}  # fmt: skip


def _source(path: Path) -> str:
    p = str(path).replace("'", "''")
    return f"read_csv('{p}', header=true, all_varchar=true, strict_mode=false, null_padding=true)"


def _read(con: duckdb.DuckDBPyConnection, path: Path, table: str, where: str = "") -> pd.DataFrame:
    """One GTFS file as strings, whitespace trimmed (Renfe pads every field), absent columns null.

    `where` may use {col} placeholders for the file's real column names.
    """
    wanted = _COLUMNS[table]
    if not path.exists():
        return pd.DataFrame({c: pd.Series(dtype="string") for c in wanted})
    src = _source(path)
    actual = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    by_clean = {a.replace("﻿", "").strip().lower(): a for a in actual}
    cols = {c: by_clean[c] for c in wanted if c in by_clean}
    select = ", ".join(f'nullif(trim("{a}"), \'\') AS "{c}"' for c, a in cols.items())
    sql = f"SELECT {select} FROM {src}"
    if where:
        sql += " WHERE " + where.format(**{c: f'trim("{a}")' for c, a in cols.items()})
    df = con.execute(sql).df()
    for c in wanted:
        df[c] = df[c].astype("string") if c in df else pd.Series(pd.NA, index=df.index, dtype="string")
    return df[wanted]


def in_bbox(stops: pd.DataFrame, bbox: tuple[float, float, float, float] = BBOX) -> pd.Series:
    lat, lon = stops["stop_lat"].astype(float), stops["stop_lon"].astype(float)
    return lat.between(bbox[0], bbox[1]) & lon.between(bbox[2], bbox[3])


def read_gtfs(directory: Path, keep_stops=in_bbox) -> Gtfs:
    """Read an unzipped GTFS feed; stop_times only for stops where keep_stops(stops) is true.

    Calls of frequency-based trips are all kept, since their times are offsets
    from the trip's first stop.
    """
    directory = Path(directory)
    con = duckdb.connect()
    g = {t: _read(con, directory / f"{t}.txt", t) for t in _COLUMNS if t != "stop_times"}
    stops = g["stops"]
    stops = stops[stops["location_type"].fillna("0").isin(["0", "4"]) & stops["stop_lat"].notna()]
    stops = stops.assign(stop_lat=stops["stop_lat"].astype(float), stop_lon=stops["stop_lon"].astype(float))
    stops = stops[keep_stops(stops)].reset_index(drop=True)
    con.register("keep_stops", stops[["stop_id"]])
    con.register("freq_trips", g["frequencies"][["trip_id"]].drop_duplicates())
    g["stop_times"] = _read(
        con, directory / "stop_times.txt", "stop_times",
        "{stop_id} IN (SELECT stop_id FROM keep_stops) OR {trip_id} IN (SELECT trip_id FROM freq_trips)",
    )  # fmt: skip
    g["stops"] = stops
    con.close()
    return Gtfs(**g)


# --------------------------------------------------------------------------
# Calendars
# --------------------------------------------------------------------------

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _gtfs_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.astype(str), format="%Y%m%d")


def active_services(calendar: pd.DataFrame, calendar_dates: pd.DataFrame, days) -> pd.DataFrame:
    """(service_id, day) for every service running on each of `days` (calendar + exceptions)."""
    days = [pd.Timestamp(d).normalize() for d in days]
    parts = [pd.DataFrame({"service_id": pd.Series(dtype="string"), "day": pd.Series(dtype="datetime64[ns]")})]
    if len(calendar):
        start, end = _gtfs_date(calendar["start_date"]), _gtfs_date(calendar["end_date"])
        for d in days:
            on = (start <= d) & (end >= d) & (calendar[_WEEKDAYS[d.weekday()]] == "1")
            parts.append(pd.DataFrame({"service_id": calendar.loc[on, "service_id"], "day": d}))
    base = pd.concat(parts, ignore_index=True)
    if len(calendar_dates):
        cd = calendar_dates.assign(day=_gtfs_date(calendar_dates["date"]))
        cd = cd[cd["day"].isin(days)]
        added = cd.loc[cd["exception_type"] == "1", ["service_id", "day"]]
        removed = cd.loc[cd["exception_type"] == "2", ["service_id", "day"]]
        base = pd.concat([base, added], ignore_index=True)
        base = base.merge(removed.assign(_removed=True), on=["service_id", "day"], how="left")
        base = base[base["_removed"].isna()].drop(columns="_removed")
    base["day"] = base["day"].astype("datetime64[ns]")
    return base.drop_duplicates(ignore_index=True)


def holiday_days(years) -> set[date]:
    """Public holidays in Barcelona: Spanish + Catalan, plus Whit Monday and La Mercè (local)."""
    out = set(holidays.country_holidays("ES", subdiv="CT", years=list(years)))
    for y in years:
        out.add(date(y, 9, 24))
        out.add(easter(y) + timedelta(days=50))  # Whit Monday
    return out


def day_type_of(day: pd.Timestamp, holiday: bool = False) -> DayType:
    if holiday or day.weekday() == 6:
        return DayType.SUNDAY_HOLIDAY
    return DayType.SATURDAY if day.weekday() == 5 else DayType.WEEKDAY


def representative_week(gtfs: Gtfs, today: date, weeks_ahead: int = 12, weeks_back: int = 52) -> date:
    """Monday of the week the day-type profile is taken from (see module docstring)."""
    used = set(gtfs.trips["service_id"].dropna())
    monday = today - timedelta(days=today.weekday())
    candidates = [monday + timedelta(weeks=i) for i in range(weeks_ahead)]
    candidates += [monday - timedelta(weeks=i) for i in range(1, weeks_back)]
    hol = holiday_days({c.year for c in candidates} | {c.year + 1 for c in candidates})
    best, best_cover = None, -1
    for m in candidates:
        days = [m + timedelta(days=i) for i in range(7)]
        act = active_services(gtfs.calendar, gtfs.calendar_dates, days)
        act = act[act["service_id"].isin(used)]
        cover = act["day"].dt.date.nunique()
        clean = not any(d in hol for d in days[:6])
        if cover == 7 and clean:
            return m
        if cover > best_cover:
            best, best_cover = m, cover
    if best is None or best_cover == 0:
        raise ValueError("no week with any service in the feed's calendar")
    print(f"  no complete holiday-free week; using {best} ({best_cover}/7 days with service)", file=sys.stderr)
    return best


# --------------------------------------------------------------------------
# Calls, directions, patterns
# --------------------------------------------------------------------------


def to_seconds(t: pd.Series) -> pd.Series:
    """'25:03:00' / '8:55:00' -> seconds after the service day's midnight (NaN when empty)."""
    parts = t.astype("string").str.split(":", expand=True)
    if parts.shape[1] < 3:
        return pd.Series(np.nan, index=t.index)
    h, m, s = (pd.to_numeric(parts[i], errors="coerce") for i in range(3))
    return h * 3600 + m * 60 + s


def stop_calls(gtfs: Gtfs) -> pd.DataFrame:
    """Every scheduled call (trip_id, stop_id, stop_sequence, secs) at a kept stop.

    Frequency-based trips are expanded: one call per vehicle at the headway.
    Missing times (non-timepoints) take the previous known time of the trip.
    """
    st = gtfs.stop_times
    calls = pd.DataFrame({
        "trip_id": st["trip_id"],
        "stop_id": st["stop_id"],
        "stop_sequence": pd.to_numeric(st["stop_sequence"]).astype("int64"),
        "secs": to_seconds(st["departure_time"].fillna(st["arrival_time"])),
    }).sort_values(["trip_id", "stop_sequence"], ignore_index=True)  # fmt: skip
    calls["secs"] = calls.groupby("trip_id")["secs"].ffill()
    calls["secs"] = calls.groupby("trip_id")["secs"].bfill()

    freq = gtfs.frequencies.dropna(subset=["trip_id"])
    if len(freq):
        fc = calls[calls["trip_id"].isin(freq["trip_id"])].copy()
        fc["offset"] = fc["secs"] - fc.groupby("trip_id")["secs"].transform("min")
        f = pd.DataFrame({
            "trip_id": freq["trip_id"],
            "start": to_seconds(freq["start_time"]),
            "end": to_seconds(freq["end_time"]),
            "headway": pd.to_numeric(freq["headway_secs"]),
        })  # fmt: skip
        f["n"] = np.ceil((f["end"] - f["start"]) / f["headway"]).clip(lower=0).astype(int)
        f = f.loc[f.index.repeat(f["n"])]
        f["k"] = f.groupby(level=0).cumcount()
        f["dep"] = f["start"] + f["k"] * f["headway"]
        fc = fc.merge(f[["trip_id", "dep"]], on="trip_id")
        fc["secs"] = fc["dep"] + fc["offset"]
        calls = pd.concat([calls[~calls["trip_id"].isin(freq["trip_id"])], fc[calls.columns]], ignore_index=True)
    calls = calls[calls["stop_id"].isin(gtfs.stops["stop_id"])]
    return calls.dropna(subset=["secs"]).reset_index(drop=True)


def assign_directions(trips: pd.DataFrame, calls: pd.DataFrame, stops: pd.DataFrame) -> pd.Series:
    """direction_id where the feed has it; otherwise 0 if the trip runs like its route's longest trip, else 1."""
    given = pd.to_numeric(trips["direction_id"], errors="coerce")
    missing = given.isna()
    if not missing.any():
        return given.astype("Int64")
    xy = stops.set_index("stop_id")[["stop_lat", "stop_lon"]]
    c = calls[calls["trip_id"].isin(trips.loc[missing, "trip_id"])].drop_duplicates(["trip_id", "stop_sequence"])
    c = c.join(xy, on="stop_id").sort_values(["trip_id", "stop_sequence"])
    g = c.groupby("trip_id")
    ends = pd.DataFrame({
        "n": g.size(),
        "lat0": g["stop_lat"].first(), "lon0": g["stop_lon"].first(),
        "lat1": g["stop_lat"].last(), "lon1": g["stop_lon"].last(),
    })  # fmt: skip
    ends = ends.join(trips.set_index("trip_id")["route_id"])
    coslat = np.cos(np.radians(41.4))
    ends["dx"] = (ends["lon1"] - ends["lon0"]) * coslat
    ends["dy"] = ends["lat1"] - ends["lat0"]
    ref = ends.reset_index().sort_values(["route_id", "n", "trip_id"], ascending=[True, False, True])
    ref = ref.drop_duplicates("route_id").set_index("route_id")[["dx", "dy"]]
    r = ref.reindex(ends["route_id"]).to_numpy()
    dot = ends["dx"].to_numpy() * r[:, 0] + ends["dy"].to_numpy() * r[:, 1]
    derived = pd.Series(np.where(dot < 0, 1, 0), index=ends.index)
    out = given.copy()
    out[missing] = trips.loc[missing, "trip_id"].map(derived).fillna(0).to_numpy()
    return out.astype("Int64")


def main_patterns(calls: pd.DataFrame, trips: pd.DataFrame) -> pd.DataFrame:
    """Most frequent stop pattern per (route_id, direction); ties go to the longer one.

    `calls` must not be frequency-expanded. Returns route_id, direction, stop_sequence, stop_id.
    """
    c = calls.merge(trips[["trip_id", "route_id", "direction"]], on="trip_id")
    c = c.sort_values(["trip_id", "stop_sequence"])
    pat = c.groupby("trip_id").agg(
        route_id=("route_id", "first"), direction=("direction", "first"),
        key=("stop_id", "|".join), n=("stop_id", "size"),
    ).reset_index()  # fmt: skip
    counts = pat.groupby(["route_id", "direction", "key", "n"], as_index=False).agg(
        trips=("trip_id", "size"), trip_id=("trip_id", "min")
    )
    best = counts.sort_values(
        ["route_id", "direction", "trips", "n", "key"], ascending=[True, True, False, False, True]
    )
    best = best.drop_duplicates(["route_id", "direction"])
    out = c[c["trip_id"].isin(best["trip_id"])]
    return out[["route_id", "direction", "stop_sequence", "stop_id"]].reset_index(drop=True)


# --------------------------------------------------------------------------
# Departures by day type
# --------------------------------------------------------------------------


def _round(x: pd.Series) -> pd.Series:
    return np.floor(x.astype(float) + 0.5).astype("int64")


def departures_by_daytype(calls: pd.DataFrame, trips: pd.DataFrame, services: pd.DataFrame) -> pd.DataFrame:
    """Mean departures per (route_id, direction, stop_id, day_type), its peak clock hour and peak count.

    `services` = (service_id, day) for the representative week; days of a type
    without any service still count in its mean.
    """
    services = services.assign(day_type=[day_type_of(d).value for d in services["day"]])
    n_days = {DayType.WEEKDAY.value: 5, DayType.SATURDAY.value: 1, DayType.SUNDAY_HOLIDAY.value: 1}
    c = calls.merge(trips[["trip_id", "route_id", "service_id", "direction"]], on="trip_id")
    c["hour"] = (c["secs"] // 3600).astype("int64") % 24
    keys = ["route_id", "direction", "stop_id"]
    per_service = c.groupby([*keys, "service_id", "hour"], as_index=False).size()
    hourly = per_service.merge(services[["service_id", "day_type"]], on="service_id")
    hourly = hourly.groupby([*keys, "day_type", "hour"], as_index=False)["size"].sum()
    hourly["mean"] = hourly["size"] / hourly["day_type"].map(n_days)

    daily = hourly.groupby([*keys, "day_type"], as_index=False)["mean"].sum()
    hourly["day_order"] = (hourly["hour"] - 4) % 24  # ties go to the earliest hour of the service day
    peak = hourly.sort_values([*keys, "day_type", "mean", "day_order"], ascending=[True] * 4 + [False, True])
    peak = peak.drop_duplicates([*keys, "day_type"])[[*keys, "day_type", "hour", "mean"]]
    out = daily.merge(peak.rename(columns={"mean": "peak_mean", "hour": "peak_hour"}), on=[*keys, "day_type"])
    out["departures"] = _round(out["mean"])
    out = out[out["departures"] > 0]
    out["departures_peak_hour"] = _round(out["peak_mean"]).clip(lower=1)
    out["departures_peak_hour"] = out[["departures_peak_hour", "departures"]].min(axis=1)
    return out[[*keys, "day_type", "departures", "departures_peak_hour", "peak_hour"]].reset_index(drop=True)


# --------------------------------------------------------------------------
# Zones
# --------------------------------------------------------------------------


def assign_zones(stations: pd.DataFrame, geometry_df: pd.DataFrame) -> pd.DataFrame:
    """Set zone_id from the barri polygon each station falls in (null outside every polygon).

    geometry_df: zone_id, wkt_wgs84 (lon/lat WKT). Points on a border take the first match.
    """
    geoms = shapely.from_wkt(geometry_df["wkt_wgs84"].to_numpy())
    tree = shapely.STRtree(geoms)
    points = shapely.points(stations["lon"].to_numpy(dtype=float), stations["lat"].to_numpy(dtype=float))
    pt_idx, geom_idx = tree.query(points, predicate="intersects")
    first = pd.Series(geom_idx, index=pt_idx).groupby(level=0).first()
    zone = pd.Series(pd.NA, index=range(len(stations)), dtype="string")
    zone.iloc[first.index] = geometry_df["zone_id"].astype("string").to_numpy()[first.to_numpy()]
    return stations.assign(zone_id=zone.to_numpy())


def load_geometry(path: Path = GEOMETRY_PATH) -> pd.DataFrame | None:
    return pd.read_parquet(path, columns=["zone_id", "wkt_wgs84"]) if path.exists() else None


def _polygon_filter(geometry_df: pd.DataFrame | None):
    """keep_stops function for read_gtfs: bbox, then inside a barri when geometry is known."""
    if geometry_df is None:
        return in_bbox

    def keep(stops: pd.DataFrame) -> pd.Series:
        box = in_bbox(stops)
        inside = pd.Series(False, index=stops.index)
        if box.any():
            s = stops[box]
            z = assign_zones(pd.DataFrame({"lat": s["stop_lat"], "lon": s["stop_lon"]}), geometry_df)
            inside[s.index] = z["zone_id"].notna().to_numpy()
        return inside

    return keep


# --------------------------------------------------------------------------
# One feed -> tables
# --------------------------------------------------------------------------


def summarise(gtfs: Gtfs, operator: str, week_start: date) -> dict[str, pd.DataFrame]:
    """Turn one feed (already restricted to Barcelona stops) into the four tables, without zone_id."""
    routes = gtfs.routes.copy()
    modes = [route_mode(operator, t) for t in routes["route_type"]]
    routes["mode"] = pd.Series([m.value if m else None for m in modes], index=routes.index, dtype="object")
    routes = routes[routes["mode"].notna()]
    trips = gtfs.trips[gtfs.trips["route_id"].isin(routes["route_id"])].reset_index(drop=True)

    base = stop_calls(replace(gtfs, frequencies=gtfs.frequencies.iloc[0:0]))
    base = base[base["trip_id"].isin(trips["trip_id"])]
    trips = trips.assign(direction=assign_directions(trips, base, gtfs.stops))
    calls = stop_calls(gtfs)
    calls = calls[calls["trip_id"].isin(trips["trip_id"])]

    days = [week_start + timedelta(days=i) for i in range(7)]
    services = active_services(gtfs.calendar, gtfs.calendar_dates, days)
    dep = departures_by_daytype(calls, trips, services)
    pattern = main_patterns(base, trips)

    served = routes[routes["route_id"].isin(dep["route_id"])].copy()
    served["code"] = served["route_short_name"].fillna(served["route_long_name"]).fillna(served["route_id"])
    lines = pd.DataFrame({
        "line_id": operator + ":" + served["route_id"],
        "code": served["code"],
        "mode": served["mode"],
        "operator": operator,
        "vehicle_type": [
            vehicle_type(operator, TransportMode(m), c, t)
            for m, c, t in zip(served["mode"], served["code"], served["route_type"])
        ],
    })  # fmt: skip

    rank = {m.value: i for i, m in enumerate(_MODE_PRIORITY)}
    stop_mode = dep[["route_id", "stop_id"]].drop_duplicates().merge(routes[["route_id", "mode"]], on="route_id")
    stop_mode = stop_mode.assign(r=stop_mode["mode"].map(rank)).sort_values("r").drop_duplicates("stop_id")
    st = gtfs.stops.merge(stop_mode[["stop_id", "mode"]], on="stop_id")
    stations = pd.DataFrame({
        "station_id": operator + ":" + st["stop_id"],
        "name": st["stop_name"],
        "mode": st["mode"],
        "operator": operator,
        "lat": st["stop_lat"].astype(float),
        "lon": st["stop_lon"].astype(float),
        "zone_id": pd.Series(pd.NA, index=st.index, dtype="string"),
    })  # fmt: skip

    line_stops = pd.DataFrame({
        "line_id": operator + ":" + pattern["route_id"],
        "direction": pattern["direction"],
        "sequence": pattern["stop_sequence"],
        "station_id": operator + ":" + pattern["stop_id"],
    })  # fmt: skip
    line_stops = line_stops[line_stops["line_id"].isin(lines["line_id"])]

    service = pd.DataFrame({
        "line_id": operator + ":" + dep["route_id"],
        "station_id": operator + ":" + dep["stop_id"],
        "direction": dep["direction"],
        "day_type": dep["day_type"],
        "departures": dep["departures"],
        "departures_peak_hour": dep["departures_peak_hour"],
        "peak_hour": dep["peak_hour"],
    })  # fmt: skip
    return {"stations": stations, "lines": lines, "line_stops": line_stops, "service_by_daytype": service}


# --------------------------------------------------------------------------
# Typing and validation
# --------------------------------------------------------------------------


def _stations_contract(zone_nullable: bool) -> schemas.Table:
    if not zone_nullable:
        return schemas.STATIONS
    cols = tuple(replace(c, nullable=True) if c.name == "zone_id" else c for c in schemas.STATIONS.columns)
    return replace(schemas.STATIONS, columns=cols)


def typed(df: pd.DataFrame, table: schemas.Table) -> pd.DataFrame:
    out = {}
    for col in table.columns:
        s = df[col.name]
        if col.dtype == schemas.DATE:
            out[col.name] = pd.to_datetime(s).astype("datetime64[ns]")
        elif col.dtype == schemas.INT:
            out[col.name] = pd.to_numeric(s).astype("Int64")
        elif col.dtype == schemas.FLOAT:
            out[col.name] = s.astype("float64")
        elif col.dtype == schemas.BOOL:
            out[col.name] = s.astype("boolean")
        else:
            out[col.name] = s.astype("string")
    return pd.DataFrame(out).reset_index(drop=True)


def finalise(parts: list[dict[str, pd.DataFrame]], geometry_df: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """Stack the feeds, set zone_id, type and validate every table."""
    tables = {k: pd.concat([p[k] for p in parts], ignore_index=True) for k in parts[0]}
    st = tables["stations"]
    dup = st[st.duplicated("station_id", keep=False)]
    if len(dup):
        raise ValueError(f"station_id collides across feeds: {sorted(dup['station_id'].unique())[:5]}")
    if geometry_df is not None:
        st = assign_zones(st.reset_index(drop=True), geometry_df)
    tables["stations"] = schemas.check(typed(st, schemas.STATIONS), _stations_contract(geometry_df is None))
    tables["lines"] = schemas.check(typed(tables["lines"], schemas.LINES), schemas.LINES)
    order = ["line_id", "direction", "sequence"]
    ls = tables["line_stops"].sort_values(order)
    tables["line_stops"] = schemas.check(typed(ls, schemas.LINE_STOPS), schemas.LINE_STOPS)
    sbd = tables["service_by_daytype"].sort_values(["line_id", "direction", "station_id", "day_type"])
    tables["service_by_daytype"] = schemas.check(typed(sbd, SERVICE_BY_DAYTYPE), SERVICE_BY_DAYTYPE)
    return tables


def expand_service_daily(service_by_daytype: pd.DataFrame, calendar_days: pd.DataFrame, start, end) -> pd.DataFrame:
    """SERVICE_DAILY rows for every day in [start, end] of `calendar_days`.

    Each day takes its day type's profile: Sundays and public holidays ->
    sunday_holiday, Saturdays -> saturday, others (bridge days included) ->
    weekday. ASSUMPTION: the current timetable is applied to every date,
    past years included; service changes since then are not modelled.
    """
    cal = calendar_days[(calendar_days["day"] >= pd.Timestamp(start)) & (calendar_days["day"] <= pd.Timestamp(end))]
    holiday = cal["public_holiday"].fillna(False).astype(bool)
    dow = cal["dow"].astype(int)
    day_type = np.where(holiday | (dow == 6), DayType.SUNDAY_HOLIDAY.value,
                        np.where(dow == 5, DayType.SATURDAY.value, DayType.WEEKDAY.value))  # fmt: skip
    days = pd.DataFrame({"day": cal["day"].to_numpy(), "day_type": day_type})
    sbd = service_by_daytype.assign(day_type=service_by_daytype["day_type"].astype(str))
    out = days.merge(sbd, on="day_type")
    out = typed(out.sort_values(["day", "line_id", "station_id", "direction"]), schemas.SERVICE_DAILY)
    return schemas.check(out, schemas.SERVICE_DAILY)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _warn_vehicle_types(lines: pd.DataFrame) -> None:
    known = set(pd.read_csv(reference.REFERENCE_DIR / "vehicle_types.csv", dtype=str)["vehicle_type"])
    unknown = sorted(set(lines["vehicle_type"]) - known)
    if unknown:
        print(f"vehicle types not in data/reference/vehicle_types.csv (no capacity yet): {unknown}", file=sys.stderr)


def build(
    feeds: tuple[Feed, ...] = FEEDS,
    raw_dir: Path = RAW_DIR,
    out_dir: Path = PROCESSED_DIR,
    today: date | None = None,
    refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    today = today or datetime.now(ZoneInfo("Europe/Madrid")).date()
    geometry_df = load_geometry()
    if geometry_df is None:
        print(f"{GEOMETRY_PATH} missing: bounding box only, zone_id left null", file=sys.stderr)
    keep = _polygon_filter(geometry_df)
    parts, manifest = [], {}
    for feed in feeds:
        directory = extract(download(feed, raw_dir, refresh))
        gtfs = read_gtfs(directory, keep)
        week = representative_week(gtfs, today)
        part = summarise(gtfs, feed.operator, week)
        parts.append(part)
        meta_path = raw_dir / feed.operator / Path(feed.filename).with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"url": feed.url}
        manifest[feed.key] = {**meta, "licence": feed.licence, "week_start": week.isoformat()}
        print(
            f"{feed.key}: week of {week}, {len(part['stations'])} stops, {len(part['lines'])} lines, "
            f"{len(part['service_by_daytype'])} service rows",
            file=sys.stderr,
        )
    tables = finalise(parts, geometry_df)
    _warn_vehicle_types(tables["lines"])
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        print(f"{name}: {len(df)} rows", file=sys.stderr)
    return tables


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="download every feed again")
    ap.add_argument("--only", nargs="+", choices=[f.key for f in FEEDS], help="feeds to build (default all)")
    ap.add_argument("--today", type=date.fromisoformat, help="reference date for the representative week")
    ap.add_argument("--service-daily", nargs=2, metavar=("START", "END"), type=date.fromisoformat,
                    help=f"also write service_daily for this range (needs {CALENDAR_PATH.name})")  # fmt: skip
    args = ap.parse_args(argv)
    feeds = tuple(f for f in FEEDS if not args.only or f.key in args.only)
    tables = build(feeds, today=args.today, refresh=args.refresh)
    if args.service_daily:
        if not CALENDAR_PATH.exists():
            raise FileNotFoundError(f"{CALENDAR_PATH} not built yet")
        daily = expand_service_daily(tables["service_by_daytype"], pd.read_parquet(CALENDAR_PATH), *args.service_daily)
        daily.to_parquet(PROCESSED_DIR / "service_daily.parquet", index=False)
        print(f"service_daily: {len(daily)} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
