# PRISA

Precio Reactivo a la Intensidad de Saturación: a day-by-day public transport
fare for Barcelona that is cheaper on days with low expected crowding.
Mobile Data Challenge 2026, team 4+1.

## Layout

```
src/prisa/
  enums.py           closed vocabularies (segments, modes, fare kinds...)
  schemas.py         every table as a column contract + validate()/check()
  geo.py             zone ids: barri 80190103 / municipality 08019
  ingest/            raw Telefónica files -> tables (python -m prisa.ingest)
    telefonica.py    mobility files M1-M3 -> trip_flows
    tourism.py       tourism files T1-T9 -> presence, stays, origin, profile
  collect.py         archives live feeds (FGC occupancy) on the home server
  pressure.py        pressure index components and weighted composite
  saturation.py      people passing / (vehicles x capacity), per stop and line
  fares.py           daily discount calendar and the resident fairness check
  uml.py             renders schemas.py as Mermaid -> docs/data-model.md
data/
  raw/               downloads, as received (git-ignored)
  interim/           cleaned but not final (git-ignored)
  processed/         one Parquet file per table in schemas.TABLES (git-ignored)
  reference/         small hand-written CSVs (fares, capacities, hotspots), committed
docs/
  data-model.md      generated UML + table reference
  sources.md         external datasets, licences, commercial-use verdicts
notebooks/           analysis notebooks (D1)
```

## Setup

```
uv sync
uv run pytest
uv run python -m prisa.uml > docs/data-model.md   # after changing schemas.py
uv run python -m prisa.ingest data/raw/telefonica --out data/processed   # once the data arrives
```

## Conventions

- Zone codes are strings. Never let pandas read them as numbers.
- Trip counts under 15 arrive as 0 and carry `censored = True`.
- Unique-visitor counts are not additive across days or zones.
- Trips between Barcelona and other municipalities appear in two files; use
  `telefonica.drop_barcelona_boundary_duplicates`.
- Telefónica data never goes into git.
