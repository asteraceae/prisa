"""Telefónica mobility files (M1-M3) -> the `trip_flows` table.

The real delivery (Nov 2026) is one folder per month with three CSVs:

    movilidad_barrios_YYYY-MM_origen.csv      M1 barri -> barri
    movilidad_mun_barrios_YYYY-MM_origen.csv  M3 barri <-> municipality ("-" on the municipality side)
    movilidad_municipios_YYYY-MM_origen.csv   M2 municipality -> municipality

covering Jan 2023 - Aug 2025, so the full conversion runs in DuckDB
(`convert`); the pandas functions handle single small files and tests.

Differences from the spec PDF, found in the data:
  * the weekday column is `day_of_week`, not `day_of_the_week`;
  * barri codes have 9 digits with the leading zero (080190101);
  * M3 repeats a key (day, segment, origin, destination) several times with
    different counts: sub-rows of a dimension that wasn't published. Summed,
    they track the M2 municipality totals; deduplicated, they
    don't. So rows are summed per key. A summed count is censored only when
    every sub-row was 0;
  * M3 at barri level captures fewer of the trips M2 reports for the same
    municipality pairs, because finer cells fall under the 15-trip threshold
    more often. Keep both sources and never add them together: use M2 for
    municipality totals and M3 for which barri a trip starts or ends in.
"""

from __future__ import annotations

import glob
from pathlib import Path

import duckdb
import pandas as pd

from prisa import geo, schemas
from prisa.enums import SEGMENT_FROM_RAW, SourceFile

SUPPRESSION_THRESHOLD = 15  # counts below this are published as 0

_BARRI_COLS = ["barrio_origen", "barrio_destino"]
RAW_COLUMNS = {
    SourceFile.BARRI_BARRI: [
        "day", "day_of_week", "month", "origen",
        "municipio_origen", "municipio_origen_name", "barrio_origen", "barrio_origen_name",
        "municipio_destino", "municipio_destino_name", "barrio_destino", "barrio_destino_name",
        "viajes",
    ],
    SourceFile.MUNI_MUNI: [
        "day", "day_of_week", "month", "origen",
        "municipio_origen", "municipio_origen_name",
        "municipio_destino", "municipio_destino_name",
        "viajes",
    ],
}  # fmt: skip
RAW_COLUMNS[SourceFile.BARRI_MUNI] = RAW_COLUMNS[SourceFile.BARRI_BARRI]

# file name prefix -> source; longest first so "movilidad_mun_barrios" wins over shorter prefixes
FILE_PREFIXES = {
    "movilidad_mun_barrios": SourceFile.BARRI_MUNI,
    "movilidad_municipios": SourceFile.MUNI_MUNI,
    "movilidad_barrios": SourceFile.BARRI_BARRI,
}
_KEY = ["day", "origin_zone_id", "dest_zone_id", "segment"]


def identify(path: Path) -> SourceFile | None:
    name = Path(path).name.lower()
    return next((s for p, s in FILE_PREFIXES.items() if name.startswith(p)), None)


def read_head(path: Path, n: int = 50_000) -> pd.DataFrame:
    """First rows of a raw file, as text, for recognising what it is."""
    if path.suffix == ".parquet":
        return pd.read_parquet(path).head(n).astype(str)
    return pd.read_csv(path, dtype=str, keep_default_na=False, nrows=n)


def infer_source(columns: list[str], sample: pd.DataFrame | None = None) -> SourceFile | None:
    """Which mobility file this is, from its columns, for files not named as in the delivery.

    No barri columns -> municipality file. Barri columns plus any end outside
    Barcelona (or a "-" barri) -> barri<->municipality file. Otherwise barri<->barri.
    Returns None for anything that isn't a mobility file.
    """
    cols = set(columns)
    if not set(RAW_COLUMNS[SourceFile.MUNI_MUNI]) - {"day_of_week"} <= cols:
        return None
    if not set(_BARRI_COLS) <= cols:
        return SourceFile.MUNI_MUNI
    if sample is None:
        raise ValueError("need rows to tell the barri<->barri file from the barri<->municipality file")
    munis = pd.concat([sample["municipio_origen"], sample["municipio_destino"]]).map(geo.municipality_code)
    barris = pd.concat([sample["barrio_origen"], sample["barrio_destino"]]).str.strip()
    if (munis != geo.BARCELONA).any() or barris.isin(geo.MISSING).any():
        return SourceFile.BARRI_MUNI
    return SourceFile.BARRI_BARRI


# --- pandas path: one file at a time ----------------------------------------


def read_raw(path: Path, source: SourceFile, **read_kwargs) -> pd.DataFrame:
    """Read one raw file with every column as text, so codes keep their leading zeros."""
    reader = pd.read_parquet if path.suffix == ".parquet" else pd.read_csv
    kwargs = {} if reader is pd.read_parquet else {"dtype": str, "keep_default_na": False}
    df = reader(path, **kwargs, **read_kwargs)
    df = df.rename(columns={"day_of_the_week": "day_of_week"})
    missing = set(RAW_COLUMNS[source]) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing expected columns {sorted(missing)}")
    return df.astype(str)


def to_trip_flows(raw: pd.DataFrame, source: SourceFile) -> pd.DataFrame:
    has_barri = all(c in raw.columns for c in _BARRI_COLS)
    out = pd.DataFrame(
        {
            "day": pd.to_datetime(raw["day"]),
            "origin_zone_id": geo.zone_id_series(
                raw["municipio_origen"], raw["barrio_origen"] if has_barri else None
            ),
            "dest_zone_id": geo.zone_id_series(
                raw["municipio_destino"], raw["barrio_destino"] if has_barri else None
            ),
            "segment": raw["origen"].map(SEGMENT_FROM_RAW).map(str, na_action="ignore").astype("string"),
            "trips": pd.to_numeric(raw["viajes"], errors="raise").astype("Int64"),
        }
    )
    unknown = raw.loc[out["segment"].isna(), "origen"].unique()
    if len(unknown):
        raise ValueError(f"unknown origen values: {list(unknown)}")
    out = out.groupby(_KEY, as_index=False, sort=False)["trips"].sum()  # M3 sub-rows, see module doc
    out["censored"] = (out["trips"] == 0).astype("boolean")
    out["source"] = pd.Series(source.value, index=out.index, dtype="string")
    out["trips"] = out["trips"].astype("Int64")
    return schemas.check(out[schemas.TRIP_FLOWS.column_names], schemas.TRIP_FLOWS)


def load_trip_flows(path: Path, source: SourceFile | None = None) -> pd.DataFrame:
    path = Path(path)
    if source is None:
        source = identify(path)
    if source is None:
        head = read_head(path)
        source = infer_source(list(head.columns), head)
        if source is None:
            raise ValueError(f"{path.name}: not a mobility file")
    return to_trip_flows(read_raw(path, source), source)


def drop_barcelona_boundary_duplicates(flows: pd.DataFrame) -> pd.DataFrame:
    """Keep only the barri-level copy of trips between Barcelona and the rest of the province.

    Use this only when you need barri detail AND municipality pairs in one
    table. M3 (barri level) undercounts M2 for the same pairs, so for
    municipality totals keep the muni_muni rows instead.
    """
    is_mm = flows["source"] == SourceFile.MUNI_MUNI.value
    touches_bcn = (flows["origin_zone_id"] == geo.BARCELONA) | (flows["dest_zone_id"] == geo.BARCELONA)
    days_with_barri_muni = flows.loc[flows["source"] == SourceFile.BARRI_MUNI.value, "day"].unique()
    drop = is_mm & touches_bcn & flows["day"].isin(days_with_barri_muni)
    return flows.loc[~drop].reset_index(drop=True)


# --- DuckDB path: the whole delivery ----------------------------------------


def _zone_sql(muni: str, barri: str | None) -> str:
    m = f"lpad(trim({muni}), 5, '0')"
    if barri is None:
        return m
    b = f"trim({barri})"
    return (
        f"CASE WHEN {b} IS NULL OR {b} IN ('', '-') THEN {m} "
        f"WHEN length({b}) = 8 THEN '0' || {b} ELSE {b} END"
    )


def _segment_sql(col: str = "origen") -> str:
    cases = " ".join(f"WHEN '{raw}' THEN '{seg.value}'" for raw, seg in SEGMENT_FROM_RAW.items())
    return f"CASE trim({col}) {cases} END"


def flows_select_sql(files: list[str], source: SourceFile) -> str:
    """SQL turning raw CSVs of one source into trip_flows rows (plus a `year` partition column)."""
    barri = source != SourceFile.MUNI_MUNI
    file_list = ", ".join(f"'{f}'" for f in files)
    return f"""
        SELECT day, origin_zone_id, dest_zone_id, segment,
               sum(trips)::BIGINT AS trips, sum(trips) = 0 AS censored,
               '{source.value}' AS source, year(day) AS year
        FROM (
            SELECT CAST(day AS DATE) AS day,
                   {_zone_sql("municipio_origen", "barrio_origen" if barri else None)} AS origin_zone_id,
                   {_zone_sql("municipio_destino", "barrio_destino" if barri else None)} AS dest_zone_id,
                   {_segment_sql()} AS segment,
                   CAST(viajes AS BIGINT) AS trips
            FROM read_csv([{file_list}], header = true, all_varchar = true, union_by_name = true)
        )
        GROUP BY ALL
    """


def connect(memory_limit: str = "16GB", temp_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}'")
    if temp_dir is not None:
        con.execute(f"SET temp_directory = '{temp_dir}'")
    return con


def find_files(raw_root: Path) -> dict[SourceFile, list[str]]:
    files: dict[SourceFile, list[str]] = {s: [] for s in SourceFile}
    # glob (unlike Path.rglob) follows symlinked month folders
    for p in sorted(glob.glob(f"{raw_root}/**/*.csv", recursive=True)):
        source = identify(Path(p))
        if source is not None:
            files[source].append(p)
    return files


def convert(raw_root: Path, out_dir: Path, con: duckdb.DuckDBPyConnection | None = None) -> Path:
    """Write every mobility CSV under raw_root to out_dir/trip_flows/source=*/year=*/ as Parquet."""
    con = con or connect(temp_dir=Path(out_dir) / ".duckdb_tmp")
    dest = Path(out_dir) / "trip_flows"
    found = find_files(raw_root)
    if not any(found.values()):
        raise FileNotFoundError(f"no movilidad_*.csv files under {raw_root}")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for source, files in found.items():
        if not files:
            continue
        con.execute(
            f"COPY ({flows_select_sql(files, source)}) TO '{dest}' "
            "(FORMAT parquet, PARTITION_BY (source, year), COMPRESSION zstd, OVERWRITE_OR_IGNORE true)"
        )
    return dest


def trip_flows_sql(processed_dir: Path) -> str:
    """A FROM-able expression for the converted trip_flows dataset."""
    return f"read_parquet('{Path(processed_dir) / 'trip_flows'}/**/*.parquet', hive_partitioning = true)"


def check_converted(processed_dir: Path, con: duckdb.DuckDBPyConnection | None = None) -> dict[str, int]:
    """Contract checks on the converted dataset, done in SQL (too big for pandas)."""
    con = con or connect()
    rel = trip_flows_sql(processed_dir)
    segs = ", ".join(f"'{s.value}'" for s in SEGMENT_FROM_RAW.values())
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    stats = {
        "rows": q(f"SELECT count(*) FROM {rel}"),
        "bad_zone_ids": q(
            f"SELECT count(*) FROM {rel} WHERE length(origin_zone_id) NOT IN (5, 9) OR length(dest_zone_id) NOT IN (5, 9)"
        ),
        "bad_segments": q(f"SELECT count(*) FROM {rel} WHERE segment IS NULL OR segment NOT IN ({segs})"),
        "null_days": q(f"SELECT count(*) FROM {rel} WHERE day IS NULL"),
        "duplicate_keys": q(
            f"SELECT count(*) FROM (SELECT {', '.join(_KEY)}, source FROM {rel} GROUP BY ALL HAVING count(*) > 1)"
        ),
    }
    broken = {k: v for k, v in stats.items() if k != "rows" and v}
    if broken:
        raise ValueError(f"trip_flows failed checks: {broken}")
    return stats
