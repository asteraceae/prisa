"""Render the schemas as Mermaid class diagrams: `uv run python -m prisa.uml > docs/data-model.md`.

The tables come from prisa.schemas; only the relationships are listed here.
"""

from __future__ import annotations

from prisa import schemas as s
from prisa.external import geography, mitma, ridership, transit

GROUPS = {
    "Geography and hotspots": [
        s.ZONES, s.DISTRICTS, s.ZONE_POPULATION, s.ZONE_ACCOMMODATION, s.HOTSPOTS, geography.BARRI_GEOMETRY,
    ],
    "Transport network (supply)": [
        s.ZONES, s.STATIONS, s.LINES, s.LINE_STOPS, s.VEHICLE_TYPES, transit.SERVICE_BY_DAYTYPE, s.SERVICE_DAILY,
        s.MODE_SHARE,
    ],
    "Calendar and context": [s.CALENDAR_DAYS, s.EVENTS, s.WEATHER_DAILY, s.ZONES],
    "Observed demand": [
        s.ZONES, s.CALENDAR_DAYS, s.TRIP_FLOWS, s.PRESENCE_DAILY, s.PRESENCE_MONTHLY,
        s.OVERNIGHT_STAYS, s.VISITOR_ORIGIN, s.VISITOR_PROFILE, s.RIDERSHIP, s.OCCUPANCY_OBSERVATIONS,
        ridership.OCCUPANCY_HOURLY, s.EXTERNAL_FLOWS, s.ZONE_CROSSWALK, mitma.HOURLY_PROFILE,
    ],
    "Daily summaries, pressure, saturation and forecasts": [
        s.ZONES, s.TRIP_FLOWS, s.PRESENCE_DAILY, s.BARRI_DAY, s.CITY_DAY, transit.SERVICE_BY_DAYTYPE,
        s.TRANSIT_LOAD, s.PRESSURE_COMPONENTS, s.PRESSURE_INDEX, s.MODEL_RUNS, s.FORECASTS,
    ],
    "Pricing and simulation": [
        s.FORECASTS, s.FARE_PRODUCTS, s.FARE_CALENDARS, s.DAILY_FARES, s.SCENARIOS, s.SIMULATION_RESULTS,
    ],
}  # fmt: skip

# (from, multiplicity, to, multiplicity, label)
RELATIONS = [
    ("districts", "1", "zones", "*", "contains barris"),
    ("zones", "1", "zone_population", "*", "per year"),
    ("zones", "1", "zone_accommodation", "*", "per year"),
    ("zones", "1", "hotspots", "*", "located in"),
    ("zones", "1", "stations", "*", "located in"),
    ("lines", "1", "line_stops", "*", "ordered stops"),
    ("stations", "1", "line_stops", "*", ""),
    ("vehicle_types", "1", "lines", "*", "runs"),
    ("lines", "1", "service_daily", "*", "departures"),
    ("stations", "1", "service_daily", "*", ""),
    ("calendar_days", "1", "events", "*", "during"),
    ("calendar_days", "1", "weather_daily", "1", ""),
    ("zones", "0..1", "events", "*", "venue"),
    ("zones", "1", "trip_flows", "*", "origin / destination"),
    ("calendar_days", "1", "trip_flows", "*", ""),
    ("zones", "1", "presence_daily", "*", ""),
    ("zones", "1", "presence_monthly", "*", ""),
    ("zones", "1", "overnight_stays", "*", ""),
    ("zones", "1", "visitor_origin", "*", ""),
    ("zones", "1", "visitor_profile", "*", ""),
    ("zones", "1", "zone_crosswalk", "*", "maps"),
    ("zone_crosswalk", "*", "external_flows", "*", "re-zones"),
    ("trip_flows", "*", "barri_day", "*", "M1 + M3"),
    ("presence_daily", "*", "barri_day", "*", "T5"),
    ("trip_flows", "*", "city_day", "*", "M1, M2"),
    ("presence_daily", "*", "city_day", "*", "T2"),
    ("barri_day", "*", "pressure_components", "*", "density, arrivals, anomaly"),
    ("barri_day", "*", "transit_load", "*", "boardings"),
    ("service_by_daytype", "*", "transit_load", "*", "places"),
    ("transit_load", "*", "pressure_components", "*", "transit load"),
    ("barri_day", "*", "model_runs", "*", "training data"),
    ("city_day", "*", "model_runs", "*", "training data"),
    ("zones", "1", "barri_geometry", "1", "boundary"),
    ("lines", "1", "service_by_daytype", "*", "departures"),
    ("occupancy_observations", "*", "occupancy_hourly", "*", "summarised"),
    ("occupancy_hourly", "*", "transit_load", "*", "calibrates"),
    ("trip_flows", "*", "saturation", "*", "demand"),
    ("mode_share", "*", "saturation", "*", "splits demand"),
    ("ridership", "*", "saturation", "*", "calibrates"),
    ("occupancy_observations", "*", "saturation", "*", "validates"),
    ("service_daily", "*", "saturation", "*", "capacity"),
    ("saturation", "*", "pressure_components", "*", "transit load"),
    ("pressure_components", "*", "pressure_index", "*", "weighted"),
    ("model_runs", "1", "forecasts", "*", "produces"),
    ("pressure_index", "*", "model_runs", "*", "training target"),
    ("zones", "1", "pressure_index", "*", ""),
    ("forecasts", "*", "fare_calendars", "*", "priced from"),
    ("fare_calendars", "1", "daily_fares", "*", ""),
    ("fare_products", "*", "daily_fares", "*", "price x (1 - discount)"),
    ("scenarios", "1", "simulation_results", "*", ""),
    ("fare_calendars", "1", "simulation_results", "*", ""),
]


def _class_name(table: str) -> str:
    return "".join(p.capitalize() for p in table.split("_"))


def _class(t: s.Table) -> str:
    lines = [f"  class {_class_name(t.name)} {{"]
    stereo = [t.layer] + (["provisional"] if t.provisional else [])
    lines.append(f"    <<{' '.join(stereo)}>>")
    for c in t.columns:
        pk = "#" if c.name in t.primary_key else "+"
        dtype = c.enum.__name__ if c.enum else c.dtype.replace("datetime64[ns]", "date").replace("Int64", "int")
        lines.append(f"    {pk}{dtype}{'?' if c.nullable else ''} {c.name}")
    lines.append("  }")
    return "\n".join(lines)


def diagram(tables: list[s.Table]) -> str:
    names = {t.name for t in tables}
    out = ["classDiagram", "  direction LR"]
    out += [_class(t) for t in tables]
    for a, ma, b, mb, label in RELATIONS:
        if a in names and b in names:
            suffix = f" : {label}" if label else ""
            out.append(f'  {_class_name(a)} "{ma}" --> "{mb}" {_class_name(b)}{suffix}')
    return "\n".join(out)


def render() -> str:
    parts = [
        "# PRISA data model",
        "",
        "Generated from `src/prisa/schemas.py` by `uv run python -m prisa.uml`. Do not edit by hand.",
        "",
        "`#` marks primary-key columns, `?` nullable ones.",
    ]
    for title, tables in GROUPS.items():
        parts += ["", f"## {title}", "", "```mermaid", diagram(tables), "```"]
    parts += ["", "## Table reference", ""]
    for t in s.TABLES.values():
        parts += [f"### `{t.name}` ({t.layer}{', provisional' if t.provisional else ''})", ""]
        if t.doc:
            parts += [t.doc, ""]
        parts += ["| column | type | notes |", "|---|---|---|"]
        for c in t.columns:
            dtype = c.enum.__name__ if c.enum else c.dtype
            key = " (key)" if c.name in t.primary_key else ""
            null = ", nullable" if c.nullable else ""
            parts.append(f"| `{c.name}`{key} | {dtype}{null} | {c.doc} |")
        if t.sources:
            parts += ["", f"Sources: {', '.join(t.sources)}"]
        parts.append("")
    return "\n".join(parts)


if __name__ == "__main__":
    print(render())
