"""Daily context for 2023-01-01 .. 2027-12-31: calendar_days, weather_daily, events.

    uv run python -m prisa.external.calendar [--refresh]

Downloads go to data/raw/external/calendar/ and are reused unless --refresh;
tables are validated and written to data/processed/<table>.parquet.

Sources (downloaded 2026-11-05):
  holidays (Python lib, 0.108)      national + Catalan holidays, Spain(subdiv="CT").
                                    2027 matches Ordre EMT/52/2026. MIT.
  Generalitat, "Calendari de festes locals a Catalunya" (Socrata b4eh-r8up,
    analisi.transparenciacatalunya.cat). Barcelona's two local days, 2012-2026.
    Licence: Generalitat open-data terms (attribution). The 08019 rows include
    test entries and, in 2024, two stray January dates; we keep rows named
    "Barcelona" dated May/June (Pasqua Granada) or September (La Mercè).
    2023 deviates from the rule: 5 Jun (not 29 May) and Mon 25 Sep.
  School calendars (primary-level dates), DOGC orders: EDU/113/2022 (2022-23),
    EDU/133/2023 (2023-24), EDU/104/2024 (2024-25), EDF/72/2025 (2025-26),
    EDF/66/2026 (2026-27, 2027-28). The four "dies de lliure disposició" are
    chosen per school (Ordre EDF/66/2026 art. 2.4c); Barcelona's municipal school
    council only proposes common days, so they are not in school_holiday
    (FREE_DAYS_PROPOSED_BCN keeps them for reference).
  Open-Meteo Historical Weather API (archive-api.open-meteo.com), best_match:
    ECMWF IFS 9 km from 2017, ERA5 before. Land cell nearest to (41.39, 2.17)
    is 41.44, 2.20. Licence CC BY 4.0 (open-meteo.com/en/licence); free API is
    non-commercial only.
  Open Data BCN `recintes-gran-aforament` (CC BY 4.0): yearly concert days and
    spectators per venue, no dates. Used for attendance per Estadi Olímpic concert day.
  openfootball/football.json (public domain): FC Barcelona LaLiga home fixtures
    2022-23 .. 2026-27. 2026-27 dates are the published fixture list and can move.
  data/reference/events.csv: curated by hand, one source URL per row.

2027 assumptions: Barcelona's local holidays are not yet in the open dataset;
we assume Pasqua Granada (Pentecost Monday, 17 May) + 24 Sep and label them
"(assumed)". Weather is not forecastable a year ahead, so weather_daily stops at
the last observed day; use `climatology()` / `weather_proxy()` for 2027.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import holidays
import pandas as pd
from dateutil.easter import easter

from prisa import schemas
from prisa.collect import fetch

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / "external" / "calendar"
OUT_DIR = ROOT / "data" / "processed"
REFERENCE_DIR = ROOT / "data" / "reference"

START, END = date(2023, 1, 1), date(2027, 12, 31)
BARCELONA = "08019"

LOCAL_HOLIDAYS_URL = (
    "https://analisi.transparenciacatalunya.cat/resource/b4eh-r8up.json"
    "?codi_municipi_ine=08019&$limit=1000"
)
WEATHER_URL = (
    "https://archive-api.open-meteo.com/v1/archive?latitude=41.39&longitude=2.17"
    "&start_date={start}&end_date={end}&timezone=Europe%2FMadrid"
    "&daily=precipitation_sum,temperature_2m_max,temperature_2m_min"
)
WEATHER_FROM = date(1996, 1, 1)  # 30 years for the climatology
VENUES_URL = "https://opendata-ajuntament.barcelona.cat/data/dataset/ae47c8bb-d19e-43c0-8a7e-c1a78592f56c/resource/{}/download"
VENUES_RESOURCES = {
    2023: "63844549-8455-441c-b2aa-20960a34a378",
    2024: "10e5fdb9-4719-4868-af79-10edcb111ec2",
    2025: "3af456b6-7cb9-41a8-af3d-de15b82f50f0",
}
FOOTBALL_URL = "https://raw.githubusercontent.com/openfootball/football.json/master/{}/es.1.json"
FOOTBALL_SEASONS = ("2022-23", "2023-24", "2024-25", "2025-26", "2026-27")

# Inclusive school-holiday periods, primary level (infantil/primària end dates).
SCHOOL_HOLIDAYS: tuple[tuple[str, str, str], ...] = (
    ("2022-12-22", "2023-01-08", "Nadal 2022-23"),
    ("2023-04-01", "2023-04-10", "Setmana Santa 2023"),  # Sat before the order's 3 Apr start
    ("2023-06-23", "2023-09-05", "Estiu 2023"),  # classes end 22 Jun, restart 6 Sep
    ("2023-12-21", "2024-01-07", "Nadal 2023-24"),
    ("2024-03-23", "2024-04-01", "Setmana Santa 2024"),
    ("2024-06-22", "2024-09-08", "Estiu 2024"),  # end 21 Jun, start 9 Sep
    ("2024-12-21", "2025-01-07", "Nadal 2024-25"),
    ("2025-04-12", "2025-04-21", "Setmana Santa 2025"),
    ("2025-06-21", "2025-09-07", "Estiu 2025"),  # end 20 Jun, start 8 Sep
    ("2025-12-20", "2026-01-07", "Nadal 2025-26"),
    ("2026-03-28", "2026-04-06", "Setmana Santa 2026"),
    ("2026-06-20", "2026-09-07", "Estiu 2026"),  # end 19 Jun, start 8 Sep
    ("2026-12-22", "2027-01-07", "Nadal 2026-27"),
    ("2027-03-20", "2027-03-29", "Setmana Santa 2027"),
    ("2027-06-22", "2027-09-07", "Estiu 2027"),  # end 21 Jun, start 8 Sep
    ("2027-12-22", "2028-01-09", "Nadal 2027-28"),
)

# Proposed by the Consell Educatiu Municipal de Barcelona; each school decides (betevé).
FREE_DAYS_PROPOSED_BCN = (
    "2023-10-13", "2023-12-07", "2024-02-12", "2024-05-17",
    "2024-10-31", "2025-02-07", "2025-03-03", "2025-05-02",
    "2025-10-10", "2025-11-03", "2026-02-16", "2026-05-22",
    "2026-10-30", "2026-12-07", "2027-02-08", "2027-05-14",
)  # fmt: skip

# FC Barcelona home venue: (barri_number, municipality_code, expected_attendance)
CAMP_NOU, MONTJUIC, JOHAN_CRUYFF = (20, BARCELONA, None), (11, BARCELONA, 40000), (None, "08217", 6000)
MONTJUIC_FROM, CAMP_NOU_RETURN = date(2023, 7, 1), date(2025, 11, 22)
JOHAN_CRUYFF_DAYS = {date(2025, 9, 14), date(2025, 9, 21)}  # Camp Nou reopened v Athletic, 22 Nov 2025
# 40000 ≈ BSM: 15 matches, >600,000 spectators at Montjuïc in 2023; Johan Cruyff seats 6,000.


def _download(url: str, path: Path, refresh: bool = False) -> Path:
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(fetch(url, timeout=120))
        tmp.rename(path)
    return path


# --------------------------------------------------------------------------
# calendar_days
# --------------------------------------------------------------------------


def regional_holidays(years: range) -> dict[date, tuple[str, str]]:
    """{day: (name, scope)} for national and Catalan holidays."""
    ct = holidays.Spain(subdiv="CT", years=years, language="ca")
    national = holidays.Spain(years=years)
    return {d: (n, "national" if d in national else "catalan") for d, n in ct.items()}


def pentecost_monday(year: int) -> date:
    return easter(year) + timedelta(days=50)


def local_holidays(rows: list[dict], years: range) -> dict[date, tuple[str, str]]:
    """Barcelona's two local days from the Generalitat rows; assumed where unpublished."""
    out: dict[date, tuple[str, str]] = {}
    for r in rows:
        # the dataset also holds test rows under 08019 named "null" / "C. A. de Catalunya"
        if r.get("ajuntament_o_nucli_municipal") != "Barcelona" or r.get("codi_municipi_ine") != BARCELONA:
            continue
        d = date.fromisoformat(r["data"][:10])
        if d.month in (5, 6):
            out[d] = ("Dilluns de Pasqua Granada", "local")
        elif d.month == 9:
            out[d] = ("La Mercè", "local")
    published = {d.year for d in out}
    for y in years:
        if y not in published:
            out[pentecost_monday(y)] = ("Dilluns de Pasqua Granada (assumed)", "local")
            out[date(y, 9, 24)] = ("La Mercè (assumed)", "local")
    return {d: v for d, v in out.items() if d.year in years}


def bridge_days(days: pd.Series, holiday: pd.Series) -> pd.Series:
    """Working weekday whose both neighbours are off and at least one is a holiday.

    `days` must be consecutive. Covers Mon before a Tue holiday, Fri after a Thu
    holiday, and a day squeezed between two holidays (6-8 Dec).
    """
    holiday = holiday.astype(bool).reset_index(drop=True)
    off = (days.dt.day_of_week.reset_index(drop=True) >= 5) | holiday
    prev_off, next_off = off.shift(1, fill_value=False), off.shift(-1, fill_value=False)
    prev_hol, next_hol = holiday.shift(1, fill_value=False), holiday.shift(-1, fill_value=False)
    out = ~off & prev_off & next_off & (prev_hol | next_hol)
    return pd.Series(out.to_numpy(), index=days.index, dtype="boolean")


def school_holiday(days: pd.Series) -> pd.Series:
    out = pd.Series(False, index=days.index)
    for start, end, _ in SCHOOL_HOLIDAYS:
        out |= days.between(pd.Timestamp(start), pd.Timestamp(end))
    return out.astype("boolean")


def build_calendar_days(local_rows: list[dict], start: date = START, end: date = END) -> pd.DataFrame:
    years = range(start.year, end.year + 1)
    hol = regional_holidays(years)
    for d, (name, scope) in local_holidays(local_rows, years).items():
        hol[d] = (f"{hol[d][0]}; {name}", hol[d][1]) if d in hol else (name, scope)
    day = pd.Series(pd.date_range(start, end, freq="D"))
    names = day.dt.date.map(lambda d: hol.get(d, (None, None)))
    public = day.dt.date.isin(hol).astype("boolean")
    df = pd.DataFrame(
        {
            "day": day,
            "year": day.dt.year.astype("Int64"),
            "month": day.dt.month.astype("Int64"),
            "dow": day.dt.day_of_week.astype("Int64"),
            "is_weekend": (day.dt.day_of_week >= 5).astype("boolean"),
            "public_holiday": public,
            "holiday_name": names.str[0].astype("string"),
            "holiday_scope": names.str[1].astype("string"),
            "school_holiday": school_holiday(day),
            "is_bridge_day": bridge_days(day, public),
        }
    )
    return schemas.check(df, schemas.CALENDAR_DAYS)


def check_local_holidays(rows: list[dict], years: range) -> list[str]:
    """Where the published local days differ from the Pasqua Granada + 24 Sep rule."""
    got = local_holidays(rows, years)
    notes = []
    for y in years:
        rule = {pentecost_monday(y), date(y, 9, 24)}
        actual = {d for d, (n, _) in got.items() if d.year == y}
        if actual != rule:
            notes.append(f"{y}: published {sorted(map(str, actual - rule))}, rule {sorted(map(str, rule - actual))}")
    return notes


# --------------------------------------------------------------------------
# weather_daily
# --------------------------------------------------------------------------


def weather_frame(payload: dict) -> pd.DataFrame:
    d = payload["daily"]
    df = pd.DataFrame(
        {
            "day": pd.to_datetime(d["time"]),
            "precip_mm": pd.to_numeric(pd.Series(d["precipitation_sum"]), errors="coerce").astype("float64"),
            "tmax_c": pd.to_numeric(pd.Series(d["temperature_2m_max"]), errors="coerce").astype("float64"),
            "tmin_c": pd.to_numeric(pd.Series(d["temperature_2m_min"]), errors="coerce").astype("float64"),
        }
    )
    observed = df[["precip_mm", "tmax_c", "tmin_c"]].notna().any(axis=1)
    return df[observed.cummax() & observed[::-1].cummax()[::-1]].reset_index(drop=True)  # trim empty ends


def build_weather_daily(payload: dict, start: date = START) -> pd.DataFrame:
    df = weather_frame(payload)
    return schemas.check(df[df["day"] >= pd.Timestamp(start)].reset_index(drop=True), schemas.WEATHER_DAILY)


def climatology(payload: dict, years: tuple[int, int] = (1996, 2025), window: int = 15) -> pd.DataFrame:
    """Mean weather per calendar day (month, day), smoothed over `window` days.

    rain_prob = share of years with >= 1 mm that day. A 2027 proxy, not a forecast.
    """
    df = weather_frame(payload)
    df = df[df["day"].dt.year.between(*years)]
    df = df.assign(month=df["day"].dt.month, dom=df["day"].dt.day, rain=(df["precip_mm"] >= 1).astype(float))
    g = df.groupby(["month", "dom"])[["precip_mm", "tmax_c", "tmin_c", "rain"]].mean()
    half = window // 2
    if half:
        wrapped = pd.concat([g.iloc[-half:], g, g.iloc[:half]])  # year is circular
        g = wrapped.rolling(window, center=True).mean().iloc[half:-half]
    return g.rename(columns={"rain": "rain_prob"}).reset_index()


def weather_proxy(clim: pd.DataFrame, start: date = date(2027, 1, 1), end: date = END) -> pd.DataFrame:
    """weather_daily-shaped climatological values for days with no observations (e.g. 2027)."""
    day = pd.Series(pd.date_range(start, end, freq="D"), name="day")
    keys = pd.DataFrame({"day": day, "month": day.dt.month, "dom": day.dt.day})
    df = keys.merge(clim, on=["month", "dom"], how="left")[schemas.WEATHER_DAILY.column_names]
    return schemas.check(df, schemas.WEATHER_DAILY)


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


CURATED_COLUMNS = [
    "event_id", "name", "type", "start_day", "end_day",
    "barri_number", "municipality_code", "expected_attendance", "source",
]  # fmt: skip


def estadi_olimpic_per_day(venue_csvs: dict[int, pd.DataFrame]) -> dict[int, int]:
    """{year: spectators per concert day} at Estadi Olímpic from recintes-gran-aforament."""
    out = {}
    for year, df in venue_csvs.items():
        eo = df[df["Recinte"] == "Estadi Olímpic"].set_index("Indicador")["Valor"].astype(int)
        out[year] = int(round(eo["Espectadors"] / eo["Dies de concerts"]))
    return out


def fcb_venue(d: date) -> tuple[int | None, str, int | None]:
    if d in JOHAN_CRUYFF_DAYS:
        return JOHAN_CRUYFF
    if MONTJUIC_FROM <= d < CAMP_NOU_RETURN:
        return MONTJUIC
    return CAMP_NOU


def football_matches(seasons: dict[str, dict], start: date = START, end: date = END) -> pd.DataFrame:
    """FC Barcelona LaLiga home matches, in the events.csv layout."""
    rows = []
    for season, payload in seasons.items():
        for m in payload["matches"]:
            d = date.fromisoformat(m["date"])
            if m["team1"] != "FC Barcelona" or not start <= d <= end:
                continue
            barri, muni, att = fcb_venue(d)
            rows.append(
                {
                    "event_id": f"fcb_{d:%Y%m%d}",
                    "name": f"FC Barcelona v {m['team2']} (LaLiga {season})",
                    "type": "sport",
                    "start_day": d.isoformat(),
                    "end_day": d.isoformat(),
                    "barri_number": "" if barri is None else str(barri),
                    "municipality_code": muni,
                    "expected_attendance": "" if att is None else str(att),
                    "source": FOOTBALL_URL.format(season),
                }
            )
    return pd.DataFrame(rows, columns=CURATED_COLUMNS)


def build_events(
    curated: pd.DataFrame,
    football: pd.DataFrame | None = None,
    eo_per_day: dict[int, int] | None = None,
    crosswalk: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """events table. zone_id: barri via crosswalk, else municipality outside Barcelona, else null."""
    raw = pd.concat([curated, football], ignore_index=True) if football is not None else curated.copy()
    raw = raw.fillna("").astype(str)
    start = pd.to_datetime(raw["start_day"])
    attendance = pd.to_numeric(raw["expected_attendance"].replace("", None)).astype("Int64")
    if eo_per_day:
        is_eo = raw["event_id"].str.startswith("eo_") & attendance.isna()
        attendance = attendance.mask(is_eo, start.dt.year.map(eo_per_day).astype("Int64"))
    lookup = {} if crosswalk is None else dict(zip(crosswalk["barri_number"].astype(int), crosswalk["zone_id"]))
    barri = pd.to_numeric(raw["barri_number"].replace("", None)).astype("Int64")
    zone = [
        lookup.get(int(b)) if pd.notna(b) else (m if m and m != BARCELONA else None)
        for b, m in zip(barri, raw["municipality_code"])
    ]
    df = pd.DataFrame(
        {
            "event_id": raw["event_id"].astype("string"),
            "name": raw["name"].astype("string"),
            "type": raw["type"].astype("string"),
            "start_day": start,
            "end_day": pd.to_datetime(raw["end_day"]),
            "zone_id": pd.Series(zone, dtype="string"),
            "expected_attendance": attendance,
            "source": raw["source"].replace("", None).astype("string"),
        }
    )
    if (df["end_day"] < df["start_day"]).any():
        raise ValueError("events: end_day before start_day")
    return schemas.check(df.sort_values(["start_day", "event_id"]).reset_index(drop=True), schemas.EVENTS)


def read_curated(path: Path = REFERENCE_DIR / "events.csv") -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def read_crosswalk(path: Path = REFERENCE_DIR / "barri_crosswalk.csv") -> pd.DataFrame | None:
    return pd.read_csv(path, dtype=str) if path.exists() else None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def download_all(refresh: bool = False) -> dict[str, Path]:
    weather_end = date.today() - timedelta(days=2)
    paths = {
        "local": _download(LOCAL_HOLIDAYS_URL, RAW_DIR / "festes_locals_barcelona.json", refresh),
        "weather": _download(
            WEATHER_URL.format(start=WEATHER_FROM, end=weather_end), RAW_DIR / "open_meteo_daily.json", refresh
        ),
    }
    for year, rid in VENUES_RESOURCES.items():
        paths[f"venues_{year}"] = _download(VENUES_URL.format(rid), RAW_DIR / f"recintes_gran_aforament_{year}.csv", refresh)
    for season in FOOTBALL_SEASONS:
        paths[f"football_{season}"] = _download(FOOTBALL_URL.format(season), RAW_DIR / f"laliga_{season}.json", refresh)
    return paths


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refresh", action="store_true", help="download again even if raw files exist")
    args = p.parse_args(argv)
    paths = download_all(args.refresh)

    local_rows = json.loads(paths["local"].read_text())
    for note in check_local_holidays(local_rows, range(START.year, END.year + 1)):
        print(f"local holidays differ from rule: {note}")
    weather = json.loads(paths["weather"].read_text())
    venues = {y: pd.read_csv(paths[f"venues_{y}"]) for y in VENUES_RESOURCES}
    seasons = {s: json.loads(paths[f"football_{s}"].read_text()) for s in FOOTBALL_SEASONS}

    tables = {
        schemas.CALENDAR_DAYS: build_calendar_days(local_rows),
        schemas.WEATHER_DAILY: build_weather_daily(weather),
        schemas.EVENTS: build_events(
            read_curated(), football_matches(seasons), estadi_olimpic_per_day(venues), read_crosswalk()
        ),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for table, df in tables.items():
        path = OUT_DIR / f"{table.name}.parquet"
        df.to_parquet(path, index=False)
        print(f"{table.name}: {len(df)} rows -> {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
