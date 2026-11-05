"""Write the D1 notebooks (01-06) from the cells below.

    uv run python notebooks/build_notebooks.py             # write notebooks/*.ipynb (no outputs, committed)
    uv run python notebooks/build_notebooks.py --execute   # also run them into outputs/notebooks/ (git-ignored)

The notebooks are committed without outputs because their outputs show the
confidential Telefónica data. All logic lives in the prisa package; the
notebooks explain the reasoning and call it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nbformat as nbf

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "outputs" / "notebooks"

SETUP = """\
import pandas as pd
import matplotlib.pyplot as plt
from prisa import store, figures
pd.set_option("display.max_columns", 30)
pd.set_option("display.width", 160)
con = store.connect()"""

NOTEBOOKS: dict[str, list[tuple[str, str]]] = {
    "01_cleaning": [
        ("md", """# 1. Cleaning the Telefónica data

**Question:** what did Telefónica actually deliver, and what had to be fixed before it can be trusted?

The delivery is a set of mobility CSVs (one folder per month, Jan 2023 - Aug 2025, three files each) and nine small tourism files per month. `prisa.ingest` converts it into checked tables:

```
uv run python -m prisa.ingest data/raw/telefonica --out data/processed
```

Every table has a written contract in `prisa/schemas.py` (columns, types, keys, allowed values) and is checked against it when written."""),
        ("code", SETUP),
        ("md", """## What we found in the data, beyond the spec

| Finding | Consequence |
|---|---|
| Barri codes have 9 digits with the leading zero (`080190101`) | `prisa.geo` uses 9-digit barri ids; 5-digit INE codes for municipalities |
| The last two digits restart in each district (Bon Pastor = `0903`, officially barri 59) | `data/reference/barri_crosswalk.csv` maps codes to official numbers; checked by name for all 73 |
| M3 (barri <-> municipality) repeats a key several times with different counts | Rows are **summed**: the sums track M2's municipality totals, deduplicating doesn't |
| M3 captures less than M2 reports for the same municipality pairs | Finer cells fall under the 15-trip threshold more often. We never add M2 and M3 together |
| Trips < 15 are published as 0 | Every trip row carries `censored`; totals are lower bounds |
| Tourism files use `;` and write age bands as `1829` | Handled in `prisa.ingest.tourism` |
| Groups < 15 people are pooled into `NA` remainder rows, even in key columns | Split off and measured in the ingest report |"""),
        ("code", "print((store.PROCESSED / 'ingest_report.md').read_text())"),
        ("md", "## Checking the M3 sub-row decision on one month\n\nIf the repeated M3 rows are real sub-rows, summing them over barris should reproduce M2's municipality totals for trips touching Barcelona (minus what censoring hides)."),
        ("code", """check = con.execute('''
WITH m2 AS (SELECT day, segment, origin_zone_id o, dest_zone_id d, trips FROM trip_flows
            WHERE source = 'muni_muni' AND year = 2024 AND month(day) = 5
              AND (origin_zone_id = '08019' OR dest_zone_id = '08019')),
     m3 AS (SELECT day, segment,
                   CASE WHEN length(origin_zone_id) = 9 THEN '08019' ELSE origin_zone_id END o,
                   CASE WHEN length(dest_zone_id) = 9 THEN '08019' ELSE dest_zone_id END d, sum(trips) trips
            FROM trip_flows WHERE source = 'barri_muni' AND year = 2024 AND month(day) = 5 GROUP BY ALL)
SELECT corr(m2.trips, m3.trips) r, sum(m3.trips) / sum(m2.trips) captured
FROM m2 JOIN m3 USING (day, segment, o, d) WHERE m2.trips > 500''').df()
check"""),
        ("md", "## The two daily tables everything else starts from\n\n`prisa.features` builds `barri_day` (73 barris x 971 days: trips in/out by segment, visitors present by segment and type) and `city_day` (city-wide visitors and trips)."),
        ("code", "bd = store.read('barri_day'); cd = store.read('city_day')\nprint(bd.shape, cd.shape)\nbd.describe().T.round(0)"),
    ],
    "02_pressure_index": [
        ("md", """# 2. A pressure index per barri and day

**Question:** how busy is a barri on a given day, in a way that can be compared across days and argued about?

There is no agreed definition, so the index is a weighted mix of components, each ranked against the barri's own history (0-1):

| Component | Measures |
|---|---|
| `visitor_density` | non-resident visitors present / registered residents |
| `visitor_arrivals` | trips arriving in the barri made by non-residents |
| `inbound_intensity` | all trips arriving per km2 |
| `anomaly` | arrivals vs the barri's usual level for that weekday and month |
| `transit_load` | peak-hour public-transport load (see notebook 3) |

Ranking each barri against itself means every barri has its own quiet and busy days, which is what a daily discount needs. Static things (monuments, hotel beds) are not components: they don't change from day to day."""),
        ("code", SETUP + "\nfrom prisa import pressure"),
        ("code", "pd.Series(pressure.PRESETS).apply(pd.Series).fillna(0)"),
        ("code", """comps = store.read('pressure_components')
idx = store.read('pressure_index')
wide = comps.pivot_table(index=['day', 'zone_id'], columns='component', values='normalised')
wide.corr().round(2)"""),
        ("md", "Components are related but not redundant, which is why the weights matter. The preset can be changed in `pressure.PRESETS` and the pipeline re-run."),
        ("code", """geom = store.read('barri_geometry')
area = store.read('zones').set_index('zone_id')['area_km2']
dens = bd = store.read('barri_day')
dens = bd.groupby('zone_id')['visitors'].mean() / area.reindex(bd['zone_id'].unique())
figures.barri_map(dens / 1e3, geom, 'Non-resident visitors present per km2 (mean day)', label='thousand / km2');"""),
    ],
    "03_patterns": [
        ("md", "# 3. Patterns: when and where is Barcelona saturated?\n\n**Question:** which days, seasons and barris carry the load, and how full does public transport get?"),
        ("code", SETUP),
        ("code", "figures.city_visitors(store.read('city_day'));"),
        ("code", """cd = store.read('city_day'); cd['day'] = pd.to_datetime(cd['day'])
by = lambda key: (cd.groupby(key)['visitors'].mean() / cd['visitors'].mean()).round(3)
pd.DataFrame({'weekday index': by(cd['day'].dt.day_name())}).T, pd.DataFrame({'month index': by(cd['day'].dt.month)}).T"""),
        ("md", "Which weekday and which season carry the load city-wide, and does every barri follow the same pattern?"),
        ("code", """bd = store.read('barri_day'); bd['day'] = pd.to_datetime(bd['day'])
summer = bd[bd['day'].dt.month.isin([7, 8])].groupby('zone_id')['visitors'].mean()
winter = bd[bd['day'].dt.month.isin([1, 2])].groupby('zone_id')['visitors'].mean()
figures.barri_map(summer / winter, store.read('barri_geometry'), 'Summer vs winter visitors', cmap='RdBu_r', label='ratio', center=1.0);"""),
        ("md", """If coastal and monument barris peak in a different season from the city as a whole, a city-wide calendar discounts days when the hotspots are fullest - this is why notebook 6 compares three calendar designs.

## How full does public transport get?

`prisa.saturation.transit_load` estimates, per barri and day: rides starting there (trips out x segment mix x mode share from EMEF 2024 and tourist surveys) against places offered by the departures from its stops (GTFS timetables x vehicle capacity at 4 or 6 people/m2), for the busiest hour. One scale factor for riders already on board is calibrated so the FGC weekday peak matches FGC's own measured occupancy (our Geotren archive)."""),
        ("code", """import json
print(json.loads((store.PROCESSED / 'transit_load_calibration.json').read_text()))
tl = store.read('transit_load')
tl.groupby('standing_density')['ratio_peak_hour'].describe().round(3)"""),
        ("code", """t4 = tl[tl['standing_density'] == 4.0]
figures.barri_map(t4.groupby('zone_id')['ratio_peak_hour'].median(), store.read('barri_geometry'),
                  'Median peak-hour load (4 people/m2)', cmap='Reds', label='load ratio');"""),
    ],
    "04_prediction": [
        ("md", """# 4. Predicting 2027

**Question:** can we tell, a year ahead, which days will be busier than others?

The fare calendar only needs the *ranking* of days, not absolute numbers, so each series is split into a yearly level and a calendar pattern:

`log(1 + y) = level(zone, year) + pattern(zone, calendar)`

One gradient-boosting model learns the pattern for all zones from weekday, season, public and school holidays, bridge days, Easter, and events. 2027 levels are held at the latest estimate (stated assumption).

**Honest test:** fit on 2023-2024 only, predict Jan-Aug 2025, and compare with the obvious alternative (each zone's average for that month and weekday)."""),
        ("code", SETUP + "\nfrom prisa import forecast"),
        ("code", """rows = []
for target in forecast.TARGETS:
    ev = forecast.evaluate(target)
    for name in ('model', 'baseline'):
        rows.append({'target': target, 'method': name, **ev[name]})
pd.DataFrame(rows).round(3)"""),
        ("md", "`spearman` is the rank correlation of predicted vs actual days within each zone; `top_hit` / `bottom_hit` are the share of the actual busiest / quietest fifth of days that the forecast also puts there. Compare the model with the baseline on each target, above all on ranking."),
        ("code", "figures.holdout(forecast.evaluate('city_visitors'), 'City visitors, Jan-Aug 2025 (not seen in training)');"),
        ("code", """runs = store.read('model_runs')
runs[['run_id', 'target', 'train_from', 'train_to']]"""),
    ],
    "05_simulation": [
        ("md", """# 5. What would the fares do?

**Question:** if prices fall on quiet days, how much load moves off the busiest days, and what does it cost?

We can't observe how visitors react to a daily price, so the response is a scenario (A conservative, B central, C optimistic). Within each week, the flexible part of each segment's public-transport demand moves from days dearer than the week's average to cheaper ones; shifts are rebalanced so people move their day rather than appear or vanish. Residents' flexible share is 0 because T-usual is flat (70% of validations)."""),
        ("code", SETUP + "\nfrom prisa import simulate"),
        ("code", "store.read('scenarios')"),
        ("code", """s = store.read('simulation_summary')
s[['calendar_id', 'scenario_id', 'busiest_fifth_change', 'days_over_base_p90_before', 'days_over_base_p90',
   'rides_shifted_per_year', 'revenue_delta_eur', 'revenue_base_eur']].round(3)"""),
        ("code", "figures.simulation_tradeoff(s);"),
        ("md", """**Reading it:** compare how much each design takes off the busiest fifth of days with what it costs in visitor pay-per-trip revenue; much of a discount goes to people who would have travelled that day anyway. The backtest rows apply the same rules to 2023-2025 actuals.

This is the main limitation to present at the defence: daily data and an unknown response make the case suggestive, not proven; a pilot with T-mobilitat validation data would settle it."""),
    ],
    "06_calendar": [
        ("md", """# 6. The 2027 fare calendar

Three designs, all with the same rules (one price per day for everyone, only ever cheaper than today's fare, discount from 0 to 30% by expected pressure, displayed in the five levels of the team's mockup):

* **year** - city pressure ranked within the year: discounts follow the seasons;
* **month** - the same pressure ranked within each month: every month has cheap and dear days;
* **hotspots** - visitors where visitors concentrate, ranked within the month."""),
        ("code", SETUP + "\nfrom prisa import pricing, fares, reference, schemas"),
        ("code", """daily = store.read('daily_fares')
for design in pricing.DESIGNS:
    figures.calendar_heatmap(daily[daily['calendar_id'] == pricing.calendar_ids(design)[0]], f'2027 - {design} design')"""),
        ("code", """summary = store.read('fare_calendar_2027_monthly')
summary[summary['calendar_id'] == 'prisa-2027-month'].round(3)"""),
        ("md", "## Fairness checks\n\nNo product ever costs more than its published price, and resident titles (T-usual, T-jove) never cost more per trip than the T-usual reference - by construction, since flat passes don't follow the daily price. The pipeline refuses to write a calendar that breaks either rule; here they are run again explicitly."),
        ("code", """products = reference.load(schemas.FARE_PRODUCTS)
for design in pricing.DESIGNS:
    cal = daily[daily['calendar_id'] == pricing.calendar_ids(design)[0]]
    prices = fares.product_prices(cal, products)
    print(design, 'above published:', len(fares.never_above_published(prices)),
          '| resident above reference:', len(fares.check_resident_fairness(prices, 22.80, 40, ['t_usual', 't_jove'])))"""),
        ("code", """# the deliverable: calendar_2027.csv for the chosen design
chosen = daily[daily['calendar_id'] == 'prisa-2027-month'][['day', 'expected_pressure', 'tier', 'discount']]
single = products.set_index('product_id').loc['single', 'price_eur']
chosen = chosen.assign(single_ticket_eur=(single * (1 - chosen['discount'])).round(2))
out = store.OUTPUTS / 'calendar_2027.csv'; out.parent.mkdir(parents=True, exist_ok=True)
chosen.to_csv(out, index=False); out"""),
    ],
}


def write() -> list[Path]:
    paths = []
    for name, cells in NOTEBOOKS.items():
        nb = nbf.v4.new_notebook()
        nb.cells = [nbf.v4.new_markdown_cell(src) if kind == "md" else nbf.v4.new_code_cell(src) for kind, src in cells]
        for i, cell in enumerate(nb.cells):
            cell["id"] = f"{name}-{i:02d}"  # stable ids keep git diffs to real changes
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
        path = HERE / f"{name}.ipynb"
        nbf.write(nb, path)
        paths.append(path)
    return paths


def execute(paths: list[Path]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in paths:
        subprocess.run(
            [sys.executable, "-m", "jupyter", "nbconvert", "--to", "notebook", "--execute",
             "--ExecutePreprocessor.timeout=900", "--output-dir", str(OUT), str(p)],
            check=True,
        )  # fmt: skip


if __name__ == "__main__":
    written = write()
    print("\n".join(str(p) for p in written))
    if "--execute" in sys.argv:
        execute(written)
