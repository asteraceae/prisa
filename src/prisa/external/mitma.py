"""Hourly trip profiles for Barcelona from the MITMA big-data mobility study.

    uv run python -m prisa.external.mitma            # download what is missing, then build
    uv run python -m prisa.external.mitma --no-download

Telefónica gives daily trips with no hour; crowding happens in the peak hour.
This module turns a sample of MITMA's hourly origin-destination files into the
share of a day's trips that start in each hour, by Barcelona district, day
type and traveller type, plus the busiest hour's share (saturation.py's
Assumptions.peak_demand_share).

Source: Ministerio de Transportes y Movilidad Sostenible (MITMA), "Estudio de
la movilidad con Big Data", open data v2 ("estudios_basicos"), public bucket
https://movilidad-opendata.mitma.es (S3 bucket mitma-movilidad-v2), files
  estudios_basicos/por-distritos/viajes/ficheros-diarios/YYYY-MM/YYYYMMDD_Viajes_distritos.csv.gz
one per day (~180-240 MB gzip, ~20 M rows, pipe-separated) with columns
fecha|periodo|origen|destino|distancia|actividad_origen|actividad_destino|
estudio_origen_posible|estudio_destino_posible|residencia|renta|edad|sexo|viajes|viajes_km.
`periodo` is the hour the trip starts (00-23); `viajes` are trips expanded to
the population; `residencia` is the traveller's province of residence (INE
2-digit code, Spanish residents only: 52 values, no foreign countries).
Zoning: zonificacion/zonificacion_distritos/nombres_distritos.csv and
zonificacion/relacion_ine_zonificacionMitma.csv. Barcelona city is split into
its 10 INE districts, codes 0801901..0801910 ("Barcelona distrito 01"..),
which follow the city's own numbering (01 Ciutat Vella .. 10 Sant Martí).
Bucket listed and files downloaded 2026-11-05.

Licence: "Licencia de datos abiertos del MITMA" (3 Dec 2020), in the bucket
root as "LICENCIA de datos abiertos del MITMA 20201203.pdf" (read 2026-11-05).
Commercial and non-commercial reuse allowed, incl. modification and
combination; derived works may carry another licence. Conditions (sec. 2):
cite MITMA as the source, with "Basado en datos abiertos MITMA" for processed
data (ours) or "Datos abiertos MITMA" for raw data, plus a link to
https://www.mitma.es or https://movilidad-opendata.mitma.es; do not distort
the data or imply MITMA endorses the product.

Sampling design (SAMPLE_WEEKS): nine full Monday-Sunday weeks spread over the
seasons of 2023-2025 (about 200 MB per day, so ~12 GB downloaded in all),
most chosen to contain a holiday, so each yields up to 5 weekdays, a Saturday
and one or more Sundays/holidays:
  2023-03-13  late winter, plain week
  2023-05-29  Pentecost Monday (29 May, Barcelona local holiday)
  2023-08-14  August peak, Assumption (15 Aug)
  2023-12-04  Constitution (6 Dec) and Immaculate Conception (8 Dec)
  2024-06-24  Sant Joan (24 Jun), start of summer
  2024-09-23  La Mercè (24 Sep, Barcelona local holiday)
  2024-11-11  autumn, plain week
  2025-02-10  winter 2025, plain week
  2025-04-28  Labour Day (1 May); 28-29 Apr 2025 (the Iberian blackout) are
              not published, so this week has 5 days
61 days in all: 36 weekdays, 9 Saturdays, 16 Sundays/holidays. Days from
1 Jul 2025 are never sampled: MITMA changed method then and publishes only
sample days afterwards.

Each file is streamed through DuckDB and only rows whose origin OR destination
is a Barcelona district are kept (~0.6-1.0 M of ~20 M rows a day), as
data/raw/external/mitma/viajes_distritos_YYYYMMDD.parquet; the gzip is deleted.

Definitions
  day_type   sunday_holiday = Sunday, a Catalan public holiday
             (holidays.Spain(subdiv="CT")) or a Barcelona local holiday (La
             Mercè, 24 Sep, moved to Monday when it falls on a Sunday;
             Pentecost Monday); saturday = other Saturdays; weekday = the rest.
  traveller  visitor = residence outside Barcelona province (residencia != 08)
             OR either end of the trip is a `no_frecuente` place (a place the
             phone does not visit regularly), so both legs of a sightseeing trip
             count; local = everything else. Foreign tourists are not in MITMA
             v2 at all, so "visitor" here means Spanish visitors and locals on
             occasional trips.
  district   a trip counts towards every Barcelona district at either end
             (once if both ends are in the same district). scope="city" counts
             every trip touching Barcelona once (district_code null).
  share      trips starting in the hour / trips of the group, pooled over all
             sampled days of that day type (busier days weigh more).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

import duckdb
import holidays
import pandas as pd

from prisa import schemas

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / "external" / "mitma"
PROCESSED_DIR = ROOT / "data" / "processed"
FIGURE_PATH = ROOT / "docs" / "figures" / "hourly_profile_mitma.png"

BASE_URL = "https://movilidad-opendata.mitma.es/estudios_basicos/por-distritos/viajes/ficheros-diarios"
SOURCE = "mitma_distritos"
ATTRIBUTION = "Basado en datos abiertos MITMA (movilidad-opendata.mitma.es)"
METHOD_BREAK = date(2025, 7, 1)

BARCELONA_PROVINCE = "08"
DISTRICTS = {
    1: "Ciutat Vella", 2: "Eixample", 3: "Sants-Montjuïc", 4: "Les Corts", 5: "Sarrià-Sant Gervasi",
    6: "Gràcia", 7: "Horta-Guinardó", 8: "Nou Barris", 9: "Sant Andreu", 10: "Sant Martí",
}  # fmt: skip
BCN_DISTRICT_CODES = tuple(f"08019{d:02d}" for d in DISTRICTS)

# Mondays of the sampled weeks; see the module docstring.
SAMPLE_WEEKS = (
    date(2023, 3, 13), date(2023, 5, 29), date(2023, 8, 14), date(2023, 12, 4),
    date(2024, 6, 24), date(2024, 9, 23), date(2024, 11, 11),
    date(2025, 2, 10), date(2025, 4, 28),
)  # fmt: skip


class DayType(StrEnum):
    WEEKDAY = "weekday"
    SATURDAY = "saturday"
    SUNDAY_HOLIDAY = "sunday_holiday"


class Traveller(StrEnum):
    LOCAL = "local"
    VISITOR = "visitor"
    ALL = "all"


class Scope(StrEnum):
    CITY = "city"
    DISTRICT = "district"


_GROUP = (
    schemas.C("source", schemas.STR),
    schemas.C("scope", schemas.STR, "city = every trip touching Barcelona; district = an end there", enum=Scope),
    schemas.C("district_code", schemas.INT, "Barcelona district 1-10; null when scope = city", nullable=True),
    schemas.C("day_type", schemas.STR, enum=DayType),
    schemas.C("traveller", schemas.STR, "local / visitor (see module doc) / all", enum=Traveller),
)

HOURLY_PROFILE = schemas.Table(
    "hourly_profile",
    "derived",
    ("source", "scope", "district_code", "day_type", "traveller", "hour"),
    (
        *_GROUP,
        schemas.C("hour", schemas.INT, "0-23, hour the trip starts"),
        schemas.C("share", schemas.FLOAT, "share of the group's daily trips; sums to 1 per group"),
        schemas.C("trips", schemas.FLOAT, "expanded trips in the sample behind the share"),
        schemas.C("n_days", schemas.INT, "sampled days pooled into the group"),
    ),
    "Share of a day's trips starting in each hour, from MITMA hourly OD. Fills the "
    "hour Telefónica's daily flows lack.",
    sources=("MITMA movilidad-opendata",),
)

PEAK_SHARE = schemas.Table(
    "peak_share",
    "derived",
    ("source", "scope", "district_code", "day_type", "traveller"),
    (
        *_GROUP,
        schemas.C("peak_hour", schemas.INT, "busiest hour, 0-23"),
        schemas.C("peak_share", schemas.FLOAT, "its share of daily trips: Assumptions.peak_demand_share"),
        schemas.C("n_days", schemas.INT),
    ),
    "Busiest hour per hourly_profile group.",
    sources=("MITMA movilidad-opendata",),
)


# --------------------------------------------------------------------------
# Sample and download
# --------------------------------------------------------------------------


def sample_days(weeks: tuple[date, ...] = SAMPLE_WEEKS) -> list[date]:
    days = [w + timedelta(days=i) for w in weeks for i in range(7)]
    return [d for d in days if d < METHOD_BREAK]


def file_url(day: date) -> str:
    return f"{BASE_URL}/{day:%Y-%m}/{day:%Y%m%d}_Viajes_distritos.csv.gz"


def raw_path(day: date, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / f"viajes_distritos_{day:%Y%m%d}.parquet"


def _in_bcn(col: str) -> str:
    return f"{col} IN ({', '.join(repr(c) for c in BCN_DISTRICT_CODES)})"


def filter_file(src: Path, dest: Path, con: duckdb.DuckDBPyConnection | None = None) -> int:
    """Keep the rows of one MITMA daily CSV (gz or plain) with an end in Barcelona; return rows kept."""
    con = con or duckdb.connect()
    tmp = dest.with_suffix(".parquet.part")
    con.execute(
        f"""
        COPY (
            SELECT strptime(fecha, '%Y%m%d')::DATE AS fecha, periodo::INTEGER AS periodo,
                   origen, destino, distancia, actividad_origen, actividad_destino,
                   residencia, renta, edad, sexo,
                   TRY_CAST(viajes AS DOUBLE) AS viajes, TRY_CAST(viajes_km AS DOUBLE) AS viajes_km
            FROM read_csv('{src}', delim = '|', header = true, all_varchar = true)
            WHERE {_in_bcn("origen")} OR {_in_bcn("destino")}
        ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)
        """
    )
    tmp.replace(dest)
    return con.execute(f"SELECT count(*) FROM '{dest}'").fetchall()[0][0]


def _fetch(url: str, path: Path, tries: int = 3) -> None:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "prisa (UPF Mobile Data Challenge)"})
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))
        except OSError:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def download_day(day: date, raw_dir: Path = RAW_DIR) -> tuple[date, str]:
    """Download, filter and delete one day's file; skip days already on disk."""
    dest = raw_path(day, raw_dir)
    if dest.exists():
        return day, "already there"
    tmp_dir = raw_dir / "_download"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    gz = tmp_dir / f"{day:%Y%m%d}_Viajes_distritos.csv.gz"
    try:
        try:
            _fetch(file_url(day), gz)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # e.g. 28-29 Apr 2025 (blackout) were never published
                return day, "not published, skipped"
            raise
        con = duckdb.connect()
        con.execute("SET threads = 2")
        con.execute("SET enable_progress_bar = false")
        rows = filter_file(gz, dest, con)
    finally:
        gz.unlink(missing_ok=True)
    return day, f"{rows:,} rows kept"


def download_sample(days: list[date] | None = None, raw_dir: Path = RAW_DIR, workers: int = 3) -> None:
    days = sample_days() if days is None else days
    raw_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(workers) as pool:
        for day, status in pool.map(lambda d: download_day(d, raw_dir), days):
            print(f"{day}: {status}", flush=True)
    shutil.rmtree(raw_dir / "_download", ignore_errors=True)


# --------------------------------------------------------------------------
# Classify
# --------------------------------------------------------------------------


def bcn_holidays(years) -> dict[date, str]:
    """Catalan public holidays plus Barcelona's two local days."""
    out = dict(holidays.Spain(subdiv="CT", years=years))
    for y in years:
        merce = date(y, 9, 24)
        out[merce + timedelta(days=1) if merce.weekday() == 6 else merce] = "La Mercè"
        easter = (pd.Timestamp(y, 1, 1) + pd.offsets.Easter()).date()
        out[easter + timedelta(days=50)] = "Pentecost Monday (Segona Pasqua)"
    return out


def day_type(days: pd.Series) -> pd.Series:
    d = pd.to_datetime(days)
    hol = bcn_holidays(sorted(set(d.dt.year)))
    is_hol = d.dt.date.isin(set(hol))
    out = pd.Series(DayType.WEEKDAY.value, index=days.index, dtype="string")
    out[d.dt.day_of_week == 5] = DayType.SATURDAY.value
    out[(d.dt.day_of_week == 6) | is_hol] = DayType.SUNDAY_HOLIDAY.value
    return out


def traveller(df: pd.DataFrame) -> pd.Series:
    """visitor / local; see the module docstring."""
    visitor = (
        (df["residencia"] != BARCELONA_PROVINCE)
        | (df["actividad_origen"] == "no_frecuente")
        | (df["actividad_destino"] == "no_frecuente")
    )
    return visitor.map({True: Traveller.VISITOR.value, False: Traveller.LOCAL.value}).astype("string")


def district_code(zone: pd.Series) -> pd.Series:
    """MITMA zone -> Barcelona district number 1-10, null outside the city."""
    z = zone.astype("string")
    return pd.to_numeric(z.str[5:7].where(z.isin(BCN_DISTRICT_CODES)), errors="coerce").astype("Int64")


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def load_raw(raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Filtered rows summed over the dimensions we don't use (distance, income, age, sex)."""
    files = sorted(raw_dir.glob("viajes_distritos_*.parquet"))
    if not files:
        raise FileNotFoundError(f"no MITMA files in {raw_dir}; run without --no-download")
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    df = con.execute(
        f"""
        SELECT fecha, periodo, origen, destino, residencia, actividad_origen, actividad_destino,
               sum(viajes) AS viajes
        FROM read_parquet({[str(f) for f in files]})
        WHERE fecha < DATE '{METHOD_BREAK}'
        GROUP BY ALL
        """
    ).df()
    return df


def external_flows(raw: pd.DataFrame) -> pd.DataFrame:
    """Contract EXTERNAL_FLOWS at its primary key; activity is summed out (it is not in the key)."""
    keys = ["fecha", "periodo", "origen", "destino", "residencia"]
    g = raw.groupby(keys, as_index=False, observed=True)["viajes"].sum()
    out = pd.DataFrame({
        "day": pd.to_datetime(g["fecha"]),
        "hour": g["periodo"].astype("Int64"),
        "origin_ext_zone": g["origen"].astype("string"),
        "dest_ext_zone": g["destino"].astype("string"),
        "residence": g["residencia"].astype("string"),
        "activity_dest": pd.Series(pd.NA, index=g.index, dtype="string"),
        "trips": g["viajes"].astype("float64"),
        "source": pd.Series(SOURCE, index=g.index, dtype="string"),
    })  # fmt: skip
    out = out.sort_values(["day", "hour", "origin_ext_zone", "dest_ext_zone"], ignore_index=True)
    return schemas.check(out, schemas.EXTERNAL_FLOWS)


def _tagged(raw: pd.DataFrame) -> pd.DataFrame:
    """One row per (trip row, scope/district it counts towards), with day type and traveller."""
    base = pd.DataFrame({
        "fecha": raw["fecha"],
        "hour": raw["periodo"].astype("Int64"),
        "day_type": day_type(raw["fecha"]),
        "traveller": traveller(raw),
        "trips": raw["viajes"].astype("float64"),
    })  # fmt: skip
    o, d = district_code(raw["origen"]), district_code(raw["destino"])
    city = base.assign(scope=Scope.CITY.value, district_code=pd.Series(pd.NA, index=raw.index, dtype="Int64"))
    at_origin = base[o.notna()].assign(scope=Scope.DISTRICT.value, district_code=o[o.notna()])
    dest_only = d.notna() & (d != o).fillna(True)  # same-district trips already counted at the origin
    at_dest = base[dest_only].assign(scope=Scope.DISTRICT.value, district_code=d[dest_only])
    return pd.concat([city, at_origin, at_dest], ignore_index=True)


def hourly_profile(raw: pd.DataFrame) -> pd.DataFrame:
    t = _tagged(raw)
    t = pd.concat([t, t.assign(traveller=Traveller.ALL.value)], ignore_index=True)
    group = ["scope", "district_code", "day_type", "traveller"]
    n_days = t.groupby(group, dropna=False)["fecha"].nunique().rename("n_days")
    hourly = t.groupby([*group, "hour"], dropna=False)["trips"].sum()
    # every group gets all 24 hours, zero where nothing was observed
    idx = pd.MultiIndex.from_tuples(
        [(*g, h) for g in n_days.index for h in range(24)], names=[*group, "hour"]
    )  # fmt: skip
    hourly = hourly.reindex(idx, fill_value=0.0).reset_index()
    hourly["share"] = hourly["trips"] / hourly.groupby(group, dropna=False)["trips"].transform("sum")
    hourly = hourly.merge(n_days.reset_index(), on=group, how="left")
    out = pd.DataFrame({
        "source": pd.Series(SOURCE, index=hourly.index, dtype="string"),
        "scope": hourly["scope"].astype("string"),
        "district_code": hourly["district_code"].astype("Int64"),
        "day_type": hourly["day_type"].astype("string"),
        "traveller": hourly["traveller"].astype("string"),
        "hour": hourly["hour"].astype("Int64"),
        "share": hourly["share"].astype("float64"),
        "trips": hourly["trips"].astype("float64"),
        "n_days": hourly["n_days"].astype("Int64"),
    })  # fmt: skip
    out = out.sort_values([*group, "hour"], ignore_index=True, na_position="first")
    return schemas.check(out, HOURLY_PROFILE)


def peak_share(profile: pd.DataFrame) -> pd.DataFrame:
    group = ["source", "scope", "district_code", "day_type", "traveller"]
    top = profile.loc[profile.groupby(group, dropna=False)["share"].idxmax()]
    out = top[[*group, "n_days"]].assign(
        peak_hour=top["hour"].astype("Int64"), peak_share=top["share"].astype("float64")
    )[[c.name for c in PEAK_SHARE.columns]]
    return schemas.check(out.reset_index(drop=True), PEAK_SHARE)


# --------------------------------------------------------------------------
# Figure
# --------------------------------------------------------------------------


def plot(profile: pd.DataFrame, path: Path = FIGURE_PATH) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    panels = [("City-wide", Scope.CITY.value, None), ("Ciutat Vella (1)", Scope.DISTRICT.value, 1),
              ("Eixample (2)", Scope.DISTRICT.value, 2)]  # fmt: skip
    lines = [  # (day_type, traveller, colour, style)
        (DayType.WEEKDAY, Traveller.LOCAL, "#1f5fa8", "-"),
        (DayType.WEEKDAY, Traveller.VISITOR, "#1f5fa8", "--"),
        (DayType.SUNDAY_HOLIDAY, Traveller.LOCAL, "#d1603d", "-"),
        (DayType.SUNDAY_HOLIDAY, Traveller.VISITOR, "#d1603d", "--"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.6), sharey=True)
    top = 0.0
    for ax, (title, scope, dist) in zip(axes, panels):
        sel = profile[profile["scope"] == scope]
        sel = sel[sel["district_code"].isna()] if dist is None else sel[sel["district_code"] == dist]
        for dt, tr, colour, style in lines:
            s = sel[(sel["day_type"] == dt) & (sel["traveller"] == tr)].sort_values("hour")
            label = f"{'Weekday' if dt == DayType.WEEKDAY else 'Sunday / holiday'}, {tr.value}"
            ax.plot(s["hour"], s["share"] * 100, color=colour, linestyle=style, linewidth=1.8, label=label)
            top = max(top, float(s["share"].max() * 100)) if len(s) else top
        ax.set_title(title)
        ax.set_xticks(range(0, 24, 3))
        ax.set_xlim(0, 23)
        ax.set_xlabel("Hour the trip starts")
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylim(0, top * 1.08)
    axes[0].set_ylabel("Share of the day's trips (%)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, frameon=False, fontsize=9, bbox_to_anchor=(0.5, 0.045))
    n = profile[profile["scope"] == Scope.CITY.value].groupby("day_type")["n_days"].max()
    fig.suptitle("Hourly profile of trips touching Barcelona, by district, day type and traveller", fontsize=12)
    fig.text(
        0.01, 0.008,
        f"Sample: {n.get('weekday', 0)} weekdays, {n.get('sunday_holiday', 0)} Sundays/holidays, 2023-2025. "
        f"Visitor = residence outside Barcelona province or a non-frequent place at either end of the trip. "
        f"{ATTRIBUTION}.",
        fontsize=8, color="#555555",
    )  # fmt: skip
    fig.tight_layout(rect=(0, 0.11, 1, 0.96))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build(
    raw_dir: Path = RAW_DIR, out_dir: Path = PROCESSED_DIR, figure: Path | None = FIGURE_PATH
) -> dict[str, pd.DataFrame]:
    raw = load_raw(raw_dir)
    flows = external_flows(raw)
    profile = hourly_profile(raw)
    peaks = peak_share(profile)
    out_dir.mkdir(parents=True, exist_ok=True)
    flows.to_parquet(out_dir / "external_flows_mitma.parquet", index=False)
    profile.to_parquet(out_dir / "hourly_profile_mitma.parquet", index=False)
    peaks.to_parquet(out_dir / "peak_share_mitma.parquet", index=False)
    if figure is not None:
        plot(profile, figure)
    return {"external_flows": flows, "hourly_profile": profile, "peak_share": peaks}


def summary(tables: dict[str, pd.DataFrame]) -> str:
    flows, peaks = tables["external_flows"], tables["peak_share"]
    lines = [
        f"external_flows: {len(flows):,} rows, {flows['day'].nunique()} days, {flows['trips'].sum():,.0f} trips",
        f"hourly_profile: {len(tables['hourly_profile']):,} rows; peak_share: {len(peaks):,} rows",
        "",
        "peak hour and share (city-wide):",
    ]
    city = peaks[peaks["scope"] == Scope.CITY.value].sort_values(["day_type", "traveller"])
    for r in city.itertuples():
        lines.append(f"  {r.day_type:15} {r.traveller:8} {r.peak_hour:02d}h  {r.peak_share:.1%}  ({r.n_days} days)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-download", action="store_true", help="build from the Parquet already in data/raw")
    p.add_argument("--download-only", action="store_true")
    p.add_argument("--workers", type=int, default=3, help="parallel downloads")
    args = p.parse_args(argv)
    if not args.no_download:
        download_sample(workers=args.workers)
    if args.download_only:
        return 0
    print(summary(build()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
