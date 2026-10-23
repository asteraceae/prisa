"""Every table in the project, as a column-level contract.

The UML in docs/data-model.md is drawn from these definitions. Tables are
stored as Parquet under data/processed/<table name>.parquet and loaded into
pandas or DuckDB; `validate` checks a DataFrame against its contract before
it is written.

Layers:
  dimension  reference data that changes slowly (geography, network, calendar)
  fact       observations (Telefónica and external counts)
  derived    computed by us (indices, forecasts, fares)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import pandas as pd

from prisa.enums import (
    EventType,
    FareKind,
    HotspotType,
    Segment,
    SourceFile,
    TransportMode,
    ZoneLevel,
)

# pandas dtypes used across tables
STR = "string"
INT = "Int64"
FLOAT = "float64"
BOOL = "boolean"
DATE = "datetime64[ns]"


@dataclass(frozen=True)
class Column:
    name: str
    dtype: str
    doc: str = ""
    nullable: bool = False
    enum: type[StrEnum] | None = None


@dataclass(frozen=True)
class Table:
    name: str
    layer: str
    primary_key: tuple[str, ...]
    columns: tuple[Column, ...]
    doc: str = ""
    # True while the column list is a guess (Telefónica tourism files have no published schema yet)
    provisional: bool = False
    sources: tuple[str, ...] = field(default=())

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def empty(self) -> pd.DataFrame:
        return pd.DataFrame({c.name: pd.Series(dtype=c.dtype) for c in self.columns})


def C(name: str, dtype: str, doc: str = "", *, nullable: bool = False, enum=None) -> Column:
    return Column(name, dtype, doc, nullable, enum)


# --------------------------------------------------------------------------
# Dimensions: geography
# --------------------------------------------------------------------------

ZONES = Table(
    "zones",
    "dimension",
    ("zone_id",),
    (
        C("zone_id", STR, "8-digit barri code (80190103) or 5-digit INE municipality code (08019)"),
        C("level", STR, enum=ZoneLevel),
        C("name", STR),
        C("municipality_code", STR, "5-digit INE code, leading zero kept"),
        C("district_code", INT, "1-10, Barcelona barris only", nullable=True),
        C("barri_number", INT, "official 1-73 numbering, Barcelona barris only", nullable=True),
        C("area_km2", FLOAT, nullable=True),
        C("in_amb", BOOL, "member of the Àrea Metropolitana de Barcelona", nullable=True),
        C("atm_fare_zone", STR, "ATM integrated fare zone", nullable=True),
    ),
    "One row per barri of Barcelona and per municipality of the province.",
    sources=("Open Data BCN", "INE", "AMB", "ATM"),
)

DISTRICTS = Table(
    "districts",
    "dimension",
    ("district_code",),
    (C("district_code", INT), C("name", STR), C("area_km2", FLOAT, nullable=True)),
    sources=("Open Data BCN",),
)

ZONE_POPULATION = Table(
    "zone_population",
    "dimension",
    ("zone_id", "year"),
    (C("zone_id", STR), C("year", INT), C("population", INT, "registered residents (padró)")),
    sources=("Open Data BCN", "Idescat"),
)

ZONE_ACCOMMODATION = Table(
    "zone_accommodation",
    "dimension",
    ("zone_id", "year"),
    (
        C("zone_id", STR),
        C("year", INT),
        C("hotel_beds", INT, nullable=True),
        C("tourist_flat_beds", INT, "HUT places", nullable=True),
    ),
    "Tourist bed capacity per barri. Tourist flats (HUT) are published per barri; "
    "hotel places only per district, so hotel_beds is null until geocoded.",
    sources=("Open Data BCN habitatges-us-turistic", "Observatori del Turisme"),
)

HOTSPOTS = Table(
    "hotspots",
    "dimension",
    ("hotspot_id",),
    (
        C("hotspot_id", STR),
        C("name", STR),
        C("type", STR, enum=HotspotType),
        C("barri_number", INT, "official 1-73; null for city-wide (Bus Turístic)", nullable=True),
        C("zone_id", STR, "resolved from barri_number once Telefónica's numbering is confirmed", nullable=True),
        C("lat", FLOAT, nullable=True),
        C("lon", FLOAT, nullable=True),
        C("annual_visitors", INT, nullable=True),
        C("visitors_year", INT, nullable=True),
        C("source", STR, nullable=True),
    ),
    sources=("Observatori del Turisme a Barcelona",),
)

# --------------------------------------------------------------------------
# Dimensions: transport network (supply side)
# --------------------------------------------------------------------------

STATIONS = Table(
    "stations",
    "dimension",
    ("station_id",),
    (
        C("station_id", STR, "operator prefix + GTFS stop_id, e.g. tmb:1.126"),
        C("name", STR),
        C("mode", STR, enum=TransportMode),
        C("operator", STR),
        C("lat", FLOAT),
        C("lon", FLOAT),
        C("zone_id", STR, "barri or municipality the stop falls in (spatial join)"),
    ),
    sources=("GTFS feeds",),
)

LINES = Table(
    "lines",
    "dimension",
    ("line_id",),
    (
        C("line_id", STR, "operator prefix + GTFS route_id"),
        C("code", STR, "public name: L5, H12, V15, 116, T4, S1, R2..."),
        C("mode", STR, enum=TransportMode),
        C("operator", STR),
        C("vehicle_type", STR, "default vehicle; joins vehicle_types"),
    ),
    sources=("GTFS feeds",),
)

LINE_STOPS = Table(
    "line_stops",
    "dimension",
    ("line_id", "direction", "sequence"),
    (
        C("line_id", STR),
        C("direction", INT, "GTFS direction_id"),
        C("sequence", INT),
        C("station_id", STR),
    ),
    sources=("GTFS feeds",),
)

VEHICLE_TYPES = Table(
    "vehicle_types",
    "dimension",
    ("vehicle_type",),
    (
        C("vehicle_type", STR, "e.g. bus_12m, bus_18m, metro_l1, tram_citadis"),
        C("mode", STR, enum=TransportMode),
        C("capacity_seated", INT, nullable=True),
        C("capacity_total", INT, "seated + standing at the operator's nominal density"),
        C("source", STR),
    ),
)

SERVICE_DAILY = Table(
    "service_daily",
    "dimension",
    ("day", "line_id", "station_id", "direction"),
    (
        C("day", DATE),
        C("line_id", STR),
        C("station_id", STR),
        C("direction", INT),
        C("departures", INT, "scheduled vehicle departures that day"),
        C("departures_peak_hour", INT, "departures in that stop's busiest hour", nullable=True),
    ),
    "Expanded from GTFS calendars: how many vehicles actually pass each stop each day.",
    sources=("GTFS feeds",),
)

# --------------------------------------------------------------------------
# Dimensions: calendar and exogenous context
# --------------------------------------------------------------------------

CALENDAR_DAYS = Table(
    "calendar_days",
    "dimension",
    ("day",),
    (
        C("day", DATE),
        C("year", INT),
        C("month", INT),
        C("dow", INT, "0 = Monday"),
        C("is_weekend", BOOL),
        C("public_holiday", BOOL),
        C("holiday_name", STR, nullable=True),
        C("holiday_scope", STR, "national / catalan / local", nullable=True),
        C("school_holiday", BOOL),
        C("is_bridge_day", BOOL, "working day squeezed between a holiday and a weekend"),
    ),
)

EVENTS = Table(
    "events",
    "dimension",
    ("event_id",),
    (
        C("event_id", STR),
        C("name", STR),
        C("type", STR, enum=EventType),
        C("start_day", DATE),
        C("end_day", DATE),
        C("zone_id", STR, "venue barri; null for city-wide events", nullable=True),
        C("expected_attendance", INT, nullable=True),
        C("source", STR, nullable=True),
    ),
)

CRUISE_CALLS = Table(
    "cruise_calls",
    "dimension",
    ("day", "ship"),
    (
        C("day", DATE),
        C("ship", STR),
        C("terminal", STR, nullable=True),
        C("passengers", INT, "capacity or reported passengers", nullable=True),
        C("is_turnaround", BOOL, "embark/disembark port (more transit trips) vs transit call", nullable=True),
    ),
    sources=("Port de Barcelona",),
)

WEATHER_DAILY = Table(
    "weather_daily",
    "dimension",
    ("day",),
    (
        C("day", DATE),
        C("precip_mm", FLOAT, nullable=True),
        C("tmax_c", FLOAT, nullable=True),
        C("tmin_c", FLOAT, nullable=True),
    ),
    "Rain moves walkers onto transit; heat moves people to beaches.",
    sources=("AEMET OpenData",),
)

# --------------------------------------------------------------------------
# Facts: Telefónica (Smart Steps, Jan 2023 - Aug 2025)
# --------------------------------------------------------------------------

TRIP_FLOWS = Table(
    "trip_flows",
    "fact",
    ("day", "origin_zone_id", "dest_zone_id", "segment", "source"),
    (
        C("day", DATE),
        C("origin_zone_id", STR),
        C("dest_zone_id", STR),
        C("segment", STR, enum=Segment),
        C("trips", INT),
        C("censored", BOOL, "published as 0 because the true count was below 15"),
        C("source", STR, enum=SourceFile),
    ),
    "All three mobility files in one table. Intra-zone trips are not in the data.",
    sources=("Telefónica Smart Steps",),
)

PRESENCE_DAILY = Table(
    "presence_daily",
    "fact",
    ("day", "zone_id", "segment"),
    (
        C("day", DATE),
        C("zone_id", STR),
        C("segment", STR, enum=Segment),
        C("unique_visitors", INT),
        C("censored", BOOL),
    ),
    "Tourism files 2 and 5. Not additive across days or zones.",
    provisional=True,
    sources=("Telefónica Smart Steps",),
)

PRESENCE_MONTHLY = Table(
    "presence_monthly",
    "fact",
    ("year", "month", "zone_id", "segment"),
    (
        C("year", INT),
        C("month", INT),
        C("zone_id", STR),
        C("segment", STR, enum=Segment),
        C("unique_visitors", INT),
        C("censored", BOOL),
    ),
    "Tourism files 1 and 4. Never compare with the sum of daily values.",
    provisional=True,
    sources=("Telefónica Smart Steps",),
)

OVERNIGHT_STAYS = Table(
    "overnight_stays",
    "fact",
    ("year", "month", "zone_id", "segment", "nights"),
    (
        C("year", INT),
        C("month", INT),
        C("zone_id", STR),
        C("segment", STR, enum=Segment),
        C("nights", STR, "bucket label as published, e.g. '1', '2-3', '4+'"),
        C("tourists", INT),
    ),
    "Tourism file 3. Length of stay = how many days a visitor can shift a trip.",
    provisional=True,
    sources=("Telefónica Smart Steps",),
)

VISITOR_ORIGIN = Table(
    "visitor_origin",
    "fact",
    ("year", "month", "zone_id", "segment", "origin_code"),
    (
        C("year", INT),
        C("month", INT),
        C("zone_id", STR),
        C("segment", STR, enum=Segment),
        C("origin_code", STR, "municipality, province or country code"),
        C("origin_name", STR),
        C("visitors", INT),
    ),
    "Tourism files 6-8.",
    provisional=True,
    sources=("Telefónica Smart Steps",),
)

# --------------------------------------------------------------------------
# Facts: external demand data used to calibrate and to buttress past Aug 2025
# --------------------------------------------------------------------------

RIDERSHIP = Table(
    "ridership",
    "fact",
    ("period_start", "period", "statistic", "line_id", "station_id"),
    (
        C("period_start", DATE),
        C("period", STR, "half_hour / hour / day / month / year"),
        C("statistic", STR, "total / avg_weekday / boardings / alightings"),
        C("line_id", STR, nullable=True),
        C("station_id", STR, nullable=True),
        C("passengers", FLOAT, "as published"),
        C("source", STR),
    ),
    "Operator ridership at whatever grain is published (TMB: station x month, "
    "incl. average weekday; FGC: station x month; Renfe: station x 30 min, 2018). "
    "Ground truth for calibrating mode share.",
)

OCCUPANCY_OBSERVATIONS = Table(
    "occupancy_observations",
    "fact",
    ("observed_at", "line_id", "vehicle_id"),
    (
        C("observed_at", DATE, "timestamp of the snapshot"),
        C("line_id", STR),
        C("vehicle_id", STR),
        C("station_id", STR, "last/next stop at snapshot time", nullable=True),
        C("occupancy_pct", FLOAT, "mean over cars, 0-100"),
        C("source", STR),
    ),
    "Measured vehicle load. FGC Geotren publishes it live only, so we log it "
    "ourselves from now on; the only direct check on the saturation ratio.",
    sources=("FGC Geotren (CC BY 4.0)",),
)

EXTERNAL_FLOWS = Table(
    "external_flows",
    "fact",
    ("day", "hour", "origin_ext_zone", "dest_ext_zone", "residence", "source"),
    (
        C("day", DATE),
        C("hour", INT, "0-23; null where only daily totals exist", nullable=True),
        C("origin_ext_zone", STR, "zone id in the source's own zoning"),
        C("dest_ext_zone", STR),
        C("residence", STR, "source's own residence/segment label", nullable=True),
        C("activity_dest", STR, "MITMA: casa / trabajo_estudio / frecuente / no_frecuente", nullable=True),
        C("trips", FLOAT),
        C("source", STR),
    ),
    "Ministry of Transport (MITMA) big-data OD: hourly, Barcelona at district level "
    "only, Spanish residents only, daily to Jun 2025 then sample days. Gives the "
    "hourly profile Telefónica lacks.",
    sources=("MITMA movilidad-opendata",),
)

ZONE_CROSSWALK = Table(
    "zone_crosswalk",
    "dimension",
    ("ext_zone", "source", "zone_id"),
    (
        C("ext_zone", STR),
        C("source", STR),
        C("zone_id", STR),
        C("weight", FLOAT, "share of ext_zone that falls in zone_id (area or population)"),
    ),
    "Maps another source's zoning onto our barris/municipalities.",
)

# --------------------------------------------------------------------------
# Derived: pressure, saturation, forecasts
# --------------------------------------------------------------------------

PRESSURE_COMPONENTS = Table(
    "pressure_components",
    "derived",
    ("day", "zone_id", "component"),
    (
        C("day", DATE),
        C("zone_id", STR),
        C("component", STR, "see prisa.pressure.COMPONENTS"),
        C("raw", FLOAT),
        C("normalised", FLOAT, "0-1, comparable across components"),
    ),
    "Long format, so new components can be added without a schema change.",
)

PRESSURE_INDEX = Table(
    "pressure_index",
    "derived",
    ("day", "zone_id", "weights_id"),
    (
        C("day", DATE),
        C("zone_id", STR),
        C("weights_id", STR, "which weighting of the components produced this value"),
        C("value", FLOAT, "0-1"),
    ),
)

SATURATION = Table(
    "saturation",
    "derived",
    ("day", "station_id", "line_id", "direction"),
    (
        C("day", DATE),
        C("station_id", STR),
        C("line_id", STR),
        C("direction", INT),
        C("mode", STR, enum=TransportMode),
        C("demand_daily", FLOAT, "estimated boardings"),
        C("capacity_daily", FLOAT, "departures x vehicle capacity"),
        C("ratio_daily", FLOAT),
        C("ratio_peak_hour", FLOAT, "same ratio in the busiest hour", nullable=True),
    ),
    "Load factor per stop and line: people passing / (vehicles x capacity).",
)

MODEL_RUNS = Table(
    "model_runs",
    "derived",
    ("run_id",),
    (
        C("run_id", STR),
        C("created_at", DATE),
        C("target", STR, "what is predicted, e.g. pressure_index:default"),
        C("train_from", DATE),
        C("train_to", DATE),
        C("features", STR, "JSON list"),
        C("metrics", STR, "JSON dict"),
    ),
)

FORECASTS = Table(
    "forecasts",
    "derived",
    ("run_id", "day", "zone_id"),
    (
        C("run_id", STR),
        C("day", DATE),
        C("zone_id", STR),
        C("predicted", FLOAT),
        C("lower", FLOAT, nullable=True),
        C("upper", FLOAT, nullable=True),
    ),
)

# --------------------------------------------------------------------------
# Derived: pricing
# --------------------------------------------------------------------------

FARE_PRODUCTS = Table(
    "fare_products",
    "dimension",
    ("product_id", "valid_from"),
    (
        C("product_id", STR, "e.g. single, t_casual, t_usual, t_jove, t_dia"),
        C("name", STR),
        C("kind", STR, enum=FareKind),
        C("trips_included", INT, "null = unlimited", nullable=True),
        C("validity_days", INT, nullable=True),
        C("price_eur", FLOAT, "zone 1 price"),
        C("valid_from", DATE),
        C("requires_id", BOOL, "personal title tied to DNI/NIE"),
        C("daily_priced", BOOL, "follows the daily discount; flat passes (T-usual, T-jove) do not"),
        C("is_reference", BOOL, "the fare residents must never pay more than"),
        C("source", STR),
    ),
)

FARE_CALENDARS = Table(
    "fare_calendars",
    "derived",
    ("calendar_id",),
    (
        C("calendar_id", STR),
        C("year", INT),
        C("forecast_run_id", STR),
        C("policy", STR, "one_price_for_all / registered_residents_capped"),
        C("max_discount", FLOAT),
        C("created_at", DATE),
    ),
)

DAILY_FARES = Table(
    "daily_fares",
    "derived",
    ("calendar_id", "day"),
    (
        C("calendar_id", STR),
        C("day", DATE),
        C("expected_pressure", FLOAT, "city-level forecast the discount is set from"),
        C("discount", FLOAT, "0-1, continuous; discretise into tiers for display only"),
    ),
    "One discount per day for everyone. Product prices = base price x (1 - discount).",
)

SCENARIOS = Table(
    "scenarios",
    "derived",
    ("scenario_id",),
    (
        C("scenario_id", STR),
        C("name", STR),
        C("elasticity", FLOAT, "trip shift per 1% price difference between days"),
        C("flexible_share_international", FLOAT),
        C("flexible_share_national", FLOAT),
        C("flexible_share_regional", FLOAT),
        C("flexible_share_resident", FLOAT),
    ),
)

SIMULATION_RESULTS = Table(
    "simulation_results",
    "derived",
    ("scenario_id", "calendar_id", "day", "zone_id"),
    (
        C("scenario_id", STR),
        C("calendar_id", STR),
        C("day", DATE),
        C("zone_id", STR),
        C("baseline_pressure", FLOAT),
        C("simulated_pressure", FLOAT),
        C("trips_shifted", FLOAT),
        C("revenue_delta_eur", FLOAT, nullable=True),
    ),
)

TABLES: dict[str, Table] = {
    t.name: t
    for t in (
        ZONES, DISTRICTS, ZONE_POPULATION, ZONE_ACCOMMODATION, HOTSPOTS,
        STATIONS, LINES, LINE_STOPS, VEHICLE_TYPES, SERVICE_DAILY,
        CALENDAR_DAYS, EVENTS, CRUISE_CALLS, WEATHER_DAILY,
        TRIP_FLOWS, PRESENCE_DAILY, PRESENCE_MONTHLY, OVERNIGHT_STAYS, VISITOR_ORIGIN,
        RIDERSHIP, OCCUPANCY_OBSERVATIONS, EXTERNAL_FLOWS, ZONE_CROSSWALK,
        PRESSURE_COMPONENTS, PRESSURE_INDEX, SATURATION, MODEL_RUNS, FORECASTS,
        FARE_PRODUCTS, FARE_CALENDARS, DAILY_FARES, SCENARIOS, SIMULATION_RESULTS,
    )
}  # fmt: skip


def validate(df: pd.DataFrame, table: Table) -> list[str]:
    """Return every way `df` breaks `table`'s contract (empty list = valid)."""
    problems: list[str] = []
    missing = [c for c in table.column_names if c not in df.columns]
    extra = [c for c in df.columns if c not in table.column_names]
    if missing:
        problems.append(f"missing columns: {missing}")
    if extra:
        problems.append(f"unexpected columns: {extra}")

    for col in table.columns:
        if col.name not in df.columns:
            continue
        s = df[col.name]
        if col.dtype == DATE:
            # pandas 3 parses to datetime64[us]; any naive datetime resolution is fine
            if not (pd.api.types.is_datetime64_dtype(s) and getattr(s.dt, "tz", None) is None):
                problems.append(f"{col.name}: dtype {s.dtype}, expected a naive datetime")
        elif str(s.dtype) != col.dtype:
            problems.append(f"{col.name}: dtype {s.dtype}, expected {col.dtype}")
        if not col.nullable and s.isna().any():
            problems.append(f"{col.name}: {int(s.isna().sum())} nulls in a non-nullable column")
        if col.enum is not None:
            bad = set(s.dropna().unique()) - {e.value for e in col.enum}
            if bad:
                problems.append(f"{col.name}: values outside {col.enum.__name__}: {sorted(bad)[:5]}")

    if not missing and df.duplicated(list(table.primary_key)).any():
        problems.append(f"duplicate primary keys on {table.primary_key}")
    return problems


def check(df: pd.DataFrame, table: Table) -> pd.DataFrame:
    problems = validate(df, table)
    if problems:
        raise ValueError(f"{table.name} failed validation:\n  " + "\n  ".join(problems))
    return df
