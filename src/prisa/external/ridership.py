"""Observed ridership and vehicle load: the ground truth for mode/line calibration.

    uv run python -m prisa.external.ridership            # download (once) and build
    uv run python -m prisa.external.ridership --refresh  # download again

Downloads go to data/raw/external/ridership/ and are skipped when already there.
Writes, each checked with prisa.schemas.check:

  data/processed/ridership.parquet                  RIDERSHIP
  data/processed/hourly_profile_ridership.parquet   HOURLY_PROFILE (defined here)
  data/processed/occupancy_observations.parquet     OCCUPANCY_OBSERVATIONS
  data/processed/occupancy_hourly.parquet           OCCUPANCY_HOURLY (defined here)

Sources (all downloaded 2026-11-05):

  tmb_transparencia  TMB transparency portal, "Dades bàsiques i demanda"
      https://www.tmb.cat/ca/web/transparencia-tmb/serveis-publics/cartes-i-cataleg-serveis/cataleg-serveis-prestats/dades-basiques-i-demanda
      One XLSX per year (2021-2025) for metro ("Resum mensuals diaries Metro",
      later "...viatgers FMB <year>") and bus ("...Bus", later "...TB <year>").
      Metro: sheet "Mensuals" = entries per station x line x month, sheet
      "Feiners" = average entries per working day, same grain. A transfer
      station is split between its lines (hence fractional counts), so a
      station's total is the sum over its lines. Bus: one sheet, one row per
      line, two columns per month (TOTAL MENSUAL, V/D FEINERS).
      Licence: tmb.cat terms, personal / non-commercial use only. The team
      decided it may be used to calibrate this academic, non-commercial
      project; do not redistribute the files or derived per-station tables.

  fgc_demanda_mensual  "Demanda mensual de viatgers d'FGC", Generalitat de
      Catalunya open data (Socrata 4z2p-zzwm, analisi.transparenciacatalunya.cat),
      entries per station x line group (Barcelona-Vallès, Llobregat-Anoia,
      Lleida-La Pobla) x month, 2019-2025. Licence: CC BY 4.0 (Generalitat open
      data terms); attribute "Ferrocarrils de la Generalitat de Catalunya".

  renfe_franja_2018  "Volumen de Viajeros por Franja Horaria (Barcelona)",
      data.renfe.com dataset volumen-de-viajeros-por-franja-horaria-barcelona,
      boardings and alightings per Rodalies station x 30-minute slot, 2018.
      Licence: CC BY 4.0, attribute "Renfe". The file gives no date or day
      type; it is one ordinary 2018 working day (boardings = alightings =
      369,477, about a weekday's Rodalies Barcelona demand). We store it with
      the nominal date 2018-01-01 + slot time and day_type "weekday" in the
      hourly profile: the date is a placeholder, not the counting day.

  fgc_geotren  our own archive of the FGC Geotren live feed (CC BY 4.0), see
      prisa.collect and deploy/systemd/README.md. Copied read-only from the
      team server with
      rsync -a <user>@<home-server>:prisa/data/raw/collect/ data/raw/collect/

Ids. Line ids are "<operator>:<gtfs route_id>" and station ids
"<operator>:<gtfs stop_id>" once matched to data/processed/{stations,lines}.parquet
(`map_to_network`). Until then, or where no unique name match exists, they
keep the operator's own names: "tmb_name:L1", "tmb_name:PASSEIG DE GRÀCIA",
"tmb_name:116", "fgc_name:Barcelona-Vallès", "fgc_name:Pl. Catalunya",
"renfe_code:71801". Metro L9/L10 are published as "L9/10 NORD" / "L9/10 SUD"
(tmb_name:L9/10N, tmb_name:L9/10S) and can't be split into GTFS L9N/L10N.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays
import openpyxl
import pandas as pd

from prisa import schemas
from prisa.collect import parse_fgc
from prisa.schemas import FLOAT, INT, STR, C, Table

RAW = Path("data/raw/external/ridership")
COLLECT = Path("data/raw/collect")
PROCESSED = Path("data/processed")
USER_AGENT = "Mozilla/5.0 (prisa; UPF Mobile Data Challenge 2026)"
LOCAL_TZ = ZoneInfo("Europe/Madrid")

TMB_PAGE = (
    "https://www.tmb.cat/ca/web/transparencia-tmb/serveis-publics/cartes-i-cataleg-serveis/"
    "cataleg-serveis-prestats/dades-basiques-i-demanda"
)
# document-library folder of each spreadsheet family on tmb.cat
TMB_FOLDERS = {"1477764": "metro", "1477766": "bus"}
TMB_YEARS = range(2021, 2026)
FGC_URL = (
    "https://analisi.transparenciacatalunya.cat/resource/4z2p-zzwm.csv"
    "?$limit=100000&$select=l_nia_fgc,any,mes,estaci,municipi_de_l_estaci,viatgers"
)
RENFE_URL = (
    "https://data.renfe.com/dataset/9190f983-e138-42da-901a-b37205562fe4/resource/"
    "1417396e-4d6a-466a-a987-03d07aa92bed/download/barcelona_viajeros_por_franja_csv.csv"
)
RENFE_NOMINAL_DAY = pd.Timestamp("2018-01-01")
# Rodalies stations inside the municipality of Barcelona
RENFE_BCN_CITY = {"71801", "71802", "78801", "78802", "78804", "78805", "78806", "79004", "79009", "79400"}

MONTHS_CA = {
    m: i
    for i, m in enumerate(
        ["GENER", "FEBRER", "MARÇ", "ABRIL", "MAIG", "JUNY", "JULIOL", "AGOST", "SETEMBRE", "OCTUBRE", "NOVEMBRE", "DESEMBRE"],
        1,
    )
}

# --------------------------------------------------------------------------
# Contracts owned by this module
# --------------------------------------------------------------------------

HOURLY_PROFILE = Table(
    "hourly_profile_ridership",
    "derived",
    ("source", "scope", "day_type", "hour"),
    (
        C("source", STR),
        C("scope", STR, "which stations/lines, e.g. rodalies_bcn_stations"),
        C("day_type", STR, "weekday / weekend"),
        C("hour", INT, "0-23, local time"),
        C("share", FLOAT, "fraction of the day's boardings in this hour; sums to 1 per source x scope x day_type"),
    ),
    "Observed hourly demand profile, used to spread daily trips over the day.",
    sources=("Renfe volumen-de-viajeros-por-franja-horaria-barcelona (2018)",),
)

OCCUPANCY_HOURLY = Table(
    "occupancy_hourly",
    "derived",
    ("line_id", "day_type", "hour"),
    (
        C("line_id", STR),
        C("day_type", STR, "weekday / weekend (public holidays count as weekend)"),
        C("hour", INT, "0-23, Europe/Madrid local time"),
        C("mean_occupancy_pct", FLOAT, "mean over every train observation, 0-100"),
        C("p90_occupancy_pct", FLOAT),
        C("observations", INT, "train x snapshot observations"),
        C("days", INT, "distinct days observed"),
        C("first_day", schemas.DATE),
        C("last_day", schemas.DATE),
    ),
    "Summary of occupancy_observations by line x hour x day type.",
    sources=("FGC Geotren (CC BY 4.0)",),
)

# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def _get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _save(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)
    tmp.rename(path)
    return path


def tmb_links(html: str) -> dict[str, str]:
    """Local file name ("tmb_metro_2023.xlsx") -> URL, from the transparency page."""
    out = {}
    for href in re.findall(r'href="(/documents/525003/(\d+)/[^"]+?\.xlsx[^"]*)"', html):
        url, folder = href
        kind = TMB_FOLDERS.get(folder)
        year = re.search(r"(20\d\d)", url)
        if kind and year and int(year.group(1)) in TMB_YEARS:
            out[f"tmb_{kind}_{year.group(1)}.xlsx"] = "https://www.tmb.cat" + url.replace("&amp;", "&")
    return out


def download(raw: Path = RAW, refresh: bool = False) -> list[Path]:
    """Fetch every source file not already in `raw`. Returns the files present."""
    wanted = {"fgc_demanda_mensual.csv": FGC_URL, "renfe_viajeros_franja_bcn_2018.csv": RENFE_URL}
    expected_tmb = [f"tmb_{k}_{y}.xlsx" for k in TMB_FOLDERS.values() for y in TMB_YEARS]
    if refresh or not all((raw / f).exists() for f in expected_tmb):
        wanted |= tmb_links(_get(TMB_PAGE).decode("utf-8", "replace"))
    for name, url in wanted.items():
        if refresh or not (raw / name).exists():
            print(f"downloading {name}", file=sys.stderr)
            _save(raw / name, _get(url))
    return sorted(raw.glob("*.*"))


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

# TMB abbreviates differently in its two metro sheets and across years;
# station ids use the long form (the one in "Feiners").
_TMB_ABBREV = [
    (r"^R\.J\.OLIVERAS$", "RBLA. JUST OLIVERAS"),
    (r"^Z\.UNIVERSITÀRIA$", "ZONA UNIVERSITÀRIA"),
    (r"^H\. CLÍNIC$", "HOSPITAL CLÍNIC"),
    (r"^H\. DE ST\. PAU$", "HOSPITAL DE SANT PAU"),
    (r"^P\. DE GRÀCIA$|^PG\. DE GRÀCIA$", "PASSEIG DE GRÀCIA"),
    (r"^ARC DEL TRIOMF$", "ARC DE TRIOMF"),
    (r"^PUBILLA CASAS$", "PUBILLA CASES"),
    (r"^MARESME - FÒRUM$", "EL MARESME - FÒRUM"),
    (r"^FUNICULAR - PARAL·LEL$", "PARAL·LEL"),
    (r"^FUNICULAR - MIRAMAR$", "PARC DE MONTJUÏC"),
    (r"^STA\. ", "SANTA "),
    (r"^ST\. ", "SANT "),
    (r"^S\. ", "SANT "),
    (r"^PL\. ", "PLAÇA "),
    (r"^SGDA\. ", "SAGRADA "),
]


def tmb_station_name(raw: str) -> str:
    """Canonical TMB metro station name: upper case, long form, ' - ' separators."""
    s = " ".join(str(raw).split()).upper().replace("´", "'").replace("PARAL.LEL", "PARAL·LEL")
    s = re.sub(r"\s*\|\s*", " - ", s)
    s = re.sub(r"\bS\. ADRIÀ\b", "SANT ADRIÀ", s)
    for pattern, repl in _TMB_ABBREV:
        s = re.sub(pattern, repl, s)
    return s


def tmb_metro_line(header: str) -> str:
    """'LÍNIA  9/10 NORD' -> 'L9/10N'; 'FUNICULAR' -> 'FM'."""
    h = " ".join(header.split()).upper()
    if h.startswith("FUNICULAR"):
        return "FM"
    code = h.replace("LÍNIA", "").strip()
    return "L" + code.replace(" NORD", "N").replace(" SUD", "S")


def match_key(name: str) -> str:
    """Loose key for matching station names across sources (accents, case, abbreviations)."""
    s = unicodedata.normalize("NFKD", str(name).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("·", "").replace("l.l", "ll").replace("´", "'")
    for a, b in [
        (r"\bpl\.", "placa "), (r"\bpg\.", "passeig "), (r"\bav\.", "avinguda "),
        (r"\bsta\.", "santa "), (r"\bst\.", "sant "), (r"\bh\.", "hospital "), (r"\bsgda\.", "sagrada "),
        (r"\brbla\.", "rambla "),
    ]:  # fmt: skip
        s = re.sub(a, b, s)
    s = re.sub(r"\b(estacio|station|barcelona)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    # articles and "de" come and go between sources: "Pl. Catalunya" / "Plaça de Catalunya"
    return " ".join(w for w in s.split() if w not in {"de", "del", "d", "la", "el", "les", "els", "l"})


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def _ridership(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return schemas.RIDERSHIP.empty()
    df = pd.DataFrame(rows)
    df["period_start"] = pd.to_datetime(df["period_start"]).astype("datetime64[ns]")
    for c in ("period", "statistic", "line_id", "station_id", "source"):
        df[c] = df[c].astype("string")
    df["passengers"] = df["passengers"].astype(FLOAT)
    return df[schemas.RIDERSHIP.column_names]


def _number(v) -> float | None:
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_tmb_metro(path: Path, year: int) -> pd.DataFrame:
    """Station x line x month entries (sheet Mensuals) and average weekday (Feiners).

    Each sheet is a stack of blocks: a header row with the line in column B and
    month names from column D, one row per station (name in column C), then a
    TOTAL row. Totals and the summary tables under the last block are skipped.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = []
    for sheet, statistic in (("Mensuals", "total"), ("Feiners", "avg_weekday")):
        line = months = None
        for r in wb[sheet].iter_rows(values_only=True):
            r = list(r) + [None] * 4
            b, c = r[1], r[2]
            if isinstance(b, str) and b.strip():
                if r[3] == "GENER" and re.match(r"(LÍNIA|FUNICULAR)", b.strip().upper()):
                    line = tmb_metro_line(b)
                    months = {i: MONTHS_CA[v] for i, v in enumerate(r) if isinstance(v, str) and v.strip() in MONTHS_CA}
                else:  # TOTAL, titles, and anything else end the block
                    line = None
                continue
            if line is None or not isinstance(c, str) or not c.strip():
                continue
            station = tmb_station_name(c)
            for i, month in months.items():
                v = _number(r[i])
                if v is None or v == 0:
                    continue
                rows.append(
                    {
                        "period_start": datetime(year, month, 1),
                        "period": "month",
                        "statistic": statistic,
                        "line_id": f"tmb_name:{line}",
                        "station_id": f"tmb_name:{station}",
                        "passengers": v,
                        "source": "tmb_transparencia",
                    }
                )
    wb.close()
    return _ridership(rows)


def parse_tmb_bus(path: Path, year: int) -> pd.DataFrame:
    """Line x month riders and average weekday riders.

    Row 5 names each month above its first column (the second, V/D FEINERS,
    is blank there); row 6 says which column is which. Zero or empty cells
    (line not running that month) are dropped.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    head = next(i for i, r in enumerate(grid) if r[1] == "LÍNIES")
    month_row, kind_row = grid[head - 1], grid[head]
    cols, month = {}, None
    for i, (m, k) in enumerate(zip(month_row, kind_row)):
        if isinstance(m, str) and m.strip() in MONTHS_CA:
            month = MONTHS_CA[m.strip()]
        if month and isinstance(k, str):
            stat = {"TOTAL MENSUAL": "total", "V/D FEINERS": "avg_weekday"}.get(" ".join(k.split()))
            if stat:
                cols[i] = (month, stat)
    rows = []
    for r in grid[head + 1 :]:
        code = r[1]
        if code is None or str(code).strip() == "" or str(code).strip().upper() == "TOTAL":
            continue
        code = str(code).strip()
        for i, (month, stat) in cols.items():
            v = _number(r[i])
            if v is None or v == 0:
                continue
            rows.append(
                {
                    "period_start": datetime(year, month, 1),
                    "period": "month",
                    "statistic": stat,
                    "line_id": f"tmb_name:{code}",
                    "station_id": None,
                    "passengers": v,
                    "source": "tmb_transparencia",
                }
            )
    return _ridership(rows)


def parse_fgc_demand(path: Path) -> pd.DataFrame:
    """Station x line group x month entries, from the Socrata CSV."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    month = df["mes"].str.strip().str.upper().map(MONTHS_CA)
    if month.isna().any():
        raise ValueError(f"FGC: unknown months {sorted(df.loc[month.isna(), 'mes'].unique())}")
    out = pd.DataFrame(
        {
            "period_start": pd.to_datetime(dict(year=df["any"].astype(int), month=month.astype(int), day=1)),
            "period": "month",
            "statistic": "total",
            "line_id": "fgc_name:" + df["l_nia_fgc"].str.strip(),
            "station_id": "fgc_name:" + df["estaci"].str.strip(),
            "passengers": pd.to_numeric(df["viatgers"]),
            "source": "fgc_demanda_mensual",
        }
    )
    return _ridership(out.dropna(subset=["passengers"]).to_dict("records"))


def read_renfe(path: Path) -> pd.DataFrame:
    """Renfe CSV with duplicate (station, slot) rows summed; adds slot start and hour."""
    df = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df["code"] = df["CODIGO_ESTACION"].str.strip()
    df["slot"] = df["TRAMO_HORARIO"].str.strip().str[:5]
    for c in ("VIAJEROS_SUBIDOS", "VIAJEROS_BAJADOS"):
        df[c] = pd.to_numeric(df[c])
    # a few stations have two rows per slot (one per line through them): add them up
    return df.groupby(["code", "slot"], as_index=False)[["VIAJEROS_SUBIDOS", "VIAJEROS_BAJADOS"]].sum()


def parse_renfe(path: Path) -> pd.DataFrame:
    """Boardings and alightings per Rodalies station x 30 minutes (nominal date, see docstring)."""
    df = read_renfe(path)
    start = RENFE_NOMINAL_DAY + pd.to_timedelta(df["slot"] + ":00")
    rows = []
    for col, stat in (("VIAJEROS_SUBIDOS", "boardings"), ("VIAJEROS_BAJADOS", "alightings")):
        rows.append(
            pd.DataFrame(
                {
                    "period_start": start,
                    "period": "half_hour",
                    "statistic": stat,
                    "line_id": None,
                    "station_id": "renfe_code:" + df["code"],
                    "passengers": df[col],
                    "source": "renfe_franja_2018",
                }
            )
        )
    return _ridership(pd.concat(rows).to_dict("records"))


def hourly_profile(renfe: pd.DataFrame) -> pd.DataFrame:
    """Share of daily boardings per hour, all Rodalies Barcelona stations and city stations only."""
    renfe = renfe.assign(hour=renfe["slot"].str[:2].astype(int))
    out = []
    for scope, sub in (
        ("rodalies_bcn_stations", renfe),
        ("rodalies_bcn_city_stations", renfe[renfe["code"].isin(RENFE_BCN_CITY)]),
    ):
        by_hour = sub.groupby("hour")["VIAJEROS_SUBIDOS"].sum().reindex(range(24), fill_value=0)
        out.append(
            pd.DataFrame(
                {
                    "source": "renfe_franja_2018",
                    "scope": scope,
                    "day_type": "weekday",
                    "hour": by_hour.index,
                    "share": by_hour.to_numpy() / by_hour.sum(),
                }
            )
        )
    return _typed(pd.concat(out, ignore_index=True), HOURLY_PROFILE)


def _typed(df: pd.DataFrame, table: Table) -> pd.DataFrame:
    df = df.copy()
    for col in table.columns:
        if col.dtype == schemas.DATE:
            df[col.name] = pd.to_datetime(df[col.name]).astype("datetime64[ns]")
        else:
            df[col.name] = df[col.name].astype(col.dtype)
    return df[table.column_names].reset_index(drop=True)


# --------------------------------------------------------------------------
# FGC occupancy archive
# --------------------------------------------------------------------------


def load_occupancy(collect: Path = COLLECT) -> pd.DataFrame:
    """Every archived FGC snapshot, parsed with prisa.collect.parse_fgc.

    observed_at stays in UTC (naive), as the snapshot file names are.
    A train listed twice in one snapshot keeps its first row.
    """
    rows, bad = [], 0
    for path in sorted((collect / "fgc").glob("*/*.json.gz")):
        try:
            rows.extend(parse_fgc(path))
        except (ValueError, KeyError, OSError, EOFError):
            bad += 1
    if bad:
        print(f"occupancy: skipped {bad} unreadable snapshots", file=sys.stderr)
    if not rows:
        return schemas.OCCUPANCY_OBSERVATIONS.empty()
    df = pd.DataFrame(rows).drop_duplicates(["observed_at", "line_id", "vehicle_id"])
    return _typed(df, schemas.OCCUPANCY_OBSERVATIONS)


def _day_type(local: pd.Series) -> pd.Series:
    years = sorted(set(local.dt.year))
    hol = holidays.country_holidays("ES", subdiv="CT", years=years) if years else {}
    off = (local.dt.day_of_week >= 5) | local.dt.date.map(lambda d: d in hol)
    return off.map({True: "weekend", False: "weekday"})


def occupancy_summary(obs: pd.DataFrame) -> pd.DataFrame:
    """Mean and p90 occupancy by line x local hour x weekday/weekend."""
    if obs.empty:
        return OCCUPANCY_HOURLY.empty()
    local = obs["observed_at"].dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)
    df = obs.assign(hour=local.dt.hour, day=local.dt.normalize(), day_type=_day_type(local))
    g = df.groupby(["line_id", "day_type", "hour"])
    out = g["occupancy_pct"].agg(
        mean_occupancy_pct="mean",
        p90_occupancy_pct=lambda s: s.quantile(0.9),
        observations="size",
    )
    out["days"] = g["day"].nunique()
    out["first_day"] = g["day"].min()
    out["last_day"] = g["day"].max()
    return _typed(out.reset_index(), OCCUPANCY_HOURLY)


# --------------------------------------------------------------------------
# Matching to our GTFS-based ids
# --------------------------------------------------------------------------

_OPERATOR_OF_PREFIX = {"tmb_name": "tmb", "fgc_name": "fgc", "renfe_code": "renfe"}
_MODE_OF_LINE = {"tmb": ("metro", "funicular"), "fgc": ("fgc",), "renfe": ("rodalies",)}


def _unique_map(keys: pd.Series, ids: pd.Series) -> dict[str, str]:
    m = pd.DataFrame({"k": keys, "id": ids}).drop_duplicates()
    m = m[~m["k"].duplicated(keep=False)]
    return dict(zip(m["k"], m["id"]))


def map_to_network(
    df: pd.DataFrame, stations: pd.DataFrame | None, lines: pd.DataFrame | None
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Replace operator-native ids with our GTFS ids where a name matches uniquely.

    Stations match on `match_key(name)` within the operator (station_id
    prefix in `stations`) and, for TMB, within metro/funicular stops; Renfe
    stations also match on the stop code itself. Lines match on their public
    code (L1, 116, H12...). Unmatched ids are left as they were. Returns the
    mapped frame and the id mapping used.
    """
    mapping: dict[str, str] = {}
    if stations is not None and not stations.empty:
        st = stations.assign(op=stations["station_id"].str.split(":").str[0])
        for prefix, op in _OPERATOR_OF_PREFIX.items():
            cand = st[st["op"].str.lower().str.startswith(op)]
            if op == "tmb":
                cand = cand[cand["mode"].isin(_MODE_OF_LINE["tmb"])]
            by_key = _unique_map(cand["name"].map(match_key), cand["station_id"])
            by_code = _unique_map(cand["station_id"].str.split(":").str[-1], cand["station_id"])
            for sid in df["station_id"].dropna().unique():
                if not sid.startswith(prefix + ":"):
                    continue
                native = sid.split(":", 1)[1]
                hit = by_code.get(native) if op == "renfe" else None
                hit = hit or by_key.get(match_key(native))
                if hit:
                    mapping[sid] = hit
    if lines is not None and not lines.empty:
        ln = lines.assign(op=lines["line_id"].str.split(":").str[0])
        for prefix, op in _OPERATOR_OF_PREFIX.items():
            cand = ln[ln["op"].str.lower().str.startswith(op)]
            by_code = _unique_map(cand["code"].str.strip().str.upper(), cand["line_id"])
            for lid in df["line_id"].dropna().unique():
                if lid.startswith(prefix + ":"):
                    hit = by_code.get(lid.split(":", 1)[1].strip().upper())
                    if hit:
                        mapping[lid] = hit
    out = df.copy()
    for col in ("station_id", "line_id"):
        out[col] = out[col].map(lambda v: mapping.get(v, v) if isinstance(v, str) else v).astype("string")
    return out, mapping


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_ridership(raw: Path = RAW) -> pd.DataFrame:
    parts = []
    for path in sorted(raw.glob("tmb_*_*.xlsx")):
        kind, year = path.stem.split("_")[1], int(path.stem.split("_")[2])
        parts.append(parse_tmb_metro(path, year) if kind == "metro" else parse_tmb_bus(path, year))
    if (raw / "fgc_demanda_mensual.csv").exists():
        parts.append(parse_fgc_demand(raw / "fgc_demanda_mensual.csv"))
    if (raw / "renfe_viajeros_franja_bcn_2018.csv").exists():
        parts.append(parse_renfe(raw / "renfe_viajeros_franja_bcn_2018.csv"))
    return pd.concat(parts, ignore_index=True) if parts else schemas.RIDERSHIP.empty()


def _write(df: pd.DataFrame, table: Table, out: Path) -> Path:
    schemas.check(df, table)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{table.name}.parquet"
    df.to_parquet(path, index=False)
    print(f"{path}: {len(df):,} rows")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raw", type=Path, default=RAW)
    p.add_argument("--collect", type=Path, default=COLLECT, help="FGC Geotren archive (rsync it first)")
    p.add_argument("--out", type=Path, default=PROCESSED)
    p.add_argument("--refresh", action="store_true", help="download again even if files exist")
    p.add_argument("--offline", action="store_true", help="use only files already downloaded")
    args = p.parse_args(argv)

    if not args.offline:
        download(args.raw, refresh=args.refresh)

    ridership = build_ridership(args.raw)
    stations_p, lines_p = args.out / "stations.parquet", args.out / "lines.parquet"
    stations = pd.read_parquet(stations_p) if stations_p.exists() else None
    lines = pd.read_parquet(lines_p) if lines_p.exists() else None
    if stations is not None or lines is not None:
        ridership, mapping = map_to_network(ridership, stations, lines)
        n_ids = ridership["station_id"].nunique() + ridership["line_id"].nunique()
        print(f"mapped {len(mapping)} of {n_ids} ids to GTFS ids", file=sys.stderr)
    else:
        print("stations/lines.parquet not found: keeping operator-native ids", file=sys.stderr)
    _write(ridership, schemas.RIDERSHIP, args.out)

    renfe_csv = args.raw / "renfe_viajeros_franja_bcn_2018.csv"
    if renfe_csv.exists():
        _write(hourly_profile(read_renfe(renfe_csv)), HOURLY_PROFILE, args.out)

    obs = load_occupancy(args.collect)
    _write(obs, schemas.OCCUPANCY_OBSERVATIONS, args.out)
    _write(occupancy_summary(obs), OCCUPANCY_HOURLY, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
