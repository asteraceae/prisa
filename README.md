# PRISA

Precio Reactivo a la Intensidad de Saturación: a day-by-day public transport
fare for Barcelona that is cheaper on days with low expected crowding.
Mobile Data Challenge 2026, team 4+1.

## Run it

```
uv sync
uv run pytest                                   # no data needed
uv run python -m prisa.pipeline --ingest        # raw Telefónica data -> every table, forecast, calendar, figures
uv run python notebooks/build_notebooks.py --execute   # D1 notebooks with outputs, into outputs/notebooks/
```

The Telefónica delivery is confidential (NDA). Put the zip's contents in
`data/raw/telefonica/` (`movilidad/` and `turismo/` folders as delivered).
Everything under `data/raw`, `data/real`, `data/processed` and `outputs/` is
git-ignored; only code, docs and small hand-written reference CSVs are committed.

External datasets are downloaded by their builders (idempotent; add
`--external` to the pipeline to re-run them). See `docs/sources.md` for every
source and whether a commercial product may use it.

## What happens, in order

| Step | Module | Produces |
|---|---|---|
| Ingest | `prisa.ingest` | `trip_flows/` (Parquet, partitioned), tourism tables, `ingest_report.md` |
| External data | `prisa.external.*` | zones, population, tourist flats, barri boundaries, calendar, weather, events, GTFS supply, ridership, FGC occupancy, hourly profiles |
| Daily summaries | `prisa.features` | `barri_day` (73 barris x 971 days), `city_day` |
| Transit load | `prisa.saturation` | `transit_load`: peak-hour load per barri and day, at 4 and 6 people/m2, calibrated on FGC occupancy |
| Pressure | `prisa.pressure` | `pressure_components`, `pressure_index` (presets: visitors, transport, balanced) |
| Forecast | `prisa.forecast` | `forecasts` for 2027, `model_runs` with 2025-holdout metrics |
| Pricing | `prisa.pricing` | `daily_fares` for three designs (year, month, hotspots) + backtests, product prices, monthly summary |
| Simulation | `prisa.simulate` | `simulation_daily`, `simulation_summary` for scenarios A/B/C |
| Figures | `prisa.figures` | `outputs/figures/*.png` |

`prisa.store.connect()` opens every processed table as a DuckDB view;
`store.read(name)` returns one as pandas.

## Layout

```
src/prisa/
  schemas.py         every table as a column contract + validate()/check()
  enums.py, geo.py   vocabularies; zone ids (barri 080190103 / municipality 08019)
  ingest/            raw Telefónica files -> tables (DuckDB for the mobility files)
  external/          one module per external source family
  features.py ... figures.py, pipeline.py   the analysis steps above
  collect.py         archives the FGC live occupancy feed (runs on the home server)
  uml.py             renders schemas as Mermaid -> docs/data-model.md
notebooks/           01_cleaning ... 06_calendar (committed without outputs)
data/reference/      fares, vehicle capacities, hotspots, mode share, barri crosswalk, events
docs/                data-model.md (UML), sources.md, data-requests.md
deploy/systemd/      the collector's timer on the home server
```

## Conventions

- Zone codes are strings; barris have 9 digits (`080190103`), municipalities 5 (`08019`).
- Trip counts under 15 arrive as 0 and carry `censored = True`; totals are lower bounds.
- Unique-visitor counts are never summed across days or zones.
- Barri-level mobility (M3) and municipality-level (M2) describe the same trips; never add them.
- Results that depend on an assumption (2027 level, standing density, response scenarios) say so.
