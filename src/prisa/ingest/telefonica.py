"""Load Telefónica Smart Steps mobility files into the `trip_flows` table.

Raw columns are as documented in the challenge brief (Smart Steps - Challenge
2026). The tourism files have no published column list yet; their loaders
get written once we see the files.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from prisa import geo, schemas
from prisa.enums import SEGMENT_FROM_RAW, SourceFile

SUPPRESSION_THRESHOLD = 15  # counts below this are published as 0

_BARRI_COLS = ["barrio_origen", "barrio_destino"]
RAW_COLUMNS = {
    SourceFile.BARRI_BARRI: [
        "day", "day_of_the_week", "month", "origen",
        "municipio_origen", "municipio_origen_name", "barrio_origen", "barrio_origen_name",
        "municipio_destino", "municipio_destino_name", "barrio_destino", "barrio_destino_name",
        "viajes",
    ],
    SourceFile.MUNI_MUNI: [
        "day", "day_of_the_week", "month", "origen",
        "municipio_origen", "municipio_origen_name",
        "municipio_destino", "municipio_destino_name",
        "viajes",
    ],
}  # fmt: skip
RAW_COLUMNS[SourceFile.BARRI_MUNI] = RAW_COLUMNS[SourceFile.BARRI_BARRI]


def read_raw(path: Path, source: SourceFile, **read_kwargs) -> pd.DataFrame:
    """Read one raw file with every column as text, so codes keep their leading zeros."""
    reader = pd.read_parquet if path.suffix == ".parquet" else pd.read_csv
    kwargs = {} if reader is pd.read_parquet else {"dtype": str, "keep_default_na": False}
    df = reader(path, **kwargs, **read_kwargs)
    missing = set(RAW_COLUMNS[source]) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing expected columns {sorted(missing)}")
    return df.astype(str)


def to_trip_flows(raw: pd.DataFrame, source: SourceFile) -> pd.DataFrame:
    has_barri = all(c in raw.columns for c in _BARRI_COLS)
    trips = pd.to_numeric(raw["viajes"], errors="raise").astype("Int64")
    out = pd.DataFrame(
        {
            "day": pd.to_datetime(raw["day"]),
            "origin_zone_id": geo.zone_id_series(
                raw["municipio_origen"], raw["barrio_origen"] if has_barri else None
            ),
            "dest_zone_id": geo.zone_id_series(
                raw["municipio_destino"], raw["barrio_destino"] if has_barri else None
            ),
            "segment": raw["origen"].map(SEGMENT_FROM_RAW).astype("string"),
            "trips": trips,
            "censored": (trips == 0).astype("boolean"),
            "source": pd.Series(source.value, index=raw.index, dtype="string"),
        }
    )
    unknown = raw.loc[out["segment"].isna(), "origen"].unique()
    if len(unknown):
        raise ValueError(f"unknown origen values: {list(unknown)}")
    return schemas.check(out, schemas.TRIP_FLOWS)


def read_head(path: Path, n: int = 50_000) -> pd.DataFrame:
    """First rows of a raw file, as text, for recognising what it is."""
    if path.suffix == ".parquet":
        return pd.read_parquet(path).head(n).astype(str)
    return pd.read_csv(path, dtype=str, keep_default_na=False, nrows=n)


def infer_source(columns: list[str], sample: pd.DataFrame | None = None) -> SourceFile | None:
    """Which mobility file this is, from its columns (the brief gives no file names).

    No barri columns -> municipality file. Barri columns plus any end outside
    Barcelona (or a "-" barri) -> barri<->municipality file. Otherwise barri<->barri.
    Returns None for anything that isn't a mobility file.
    """
    cols = set(columns)
    if not set(RAW_COLUMNS[SourceFile.MUNI_MUNI]) <= cols:
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


def load_trip_flows(path: Path, source: SourceFile | None = None) -> pd.DataFrame:
    path = Path(path)
    if source is None:
        head = read_head(path)
        source = infer_source(list(head.columns), head)
        if source is None:
            raise ValueError(f"{path.name}: not a mobility file")
    return to_trip_flows(read_raw(path, source), source)


def drop_barcelona_boundary_duplicates(flows: pd.DataFrame) -> pd.DataFrame:
    """Keep one copy of trips between Barcelona and the rest of the province.

    Those trips are published twice: at municipality level in the muni_muni
    file and at barri level in the barri_muni file. We keep the barri-level
    rows (they roll up to the municipality) and drop the muni_muni rows that
    touch Barcelona. Only do this for days where barri_muni rows exist.
    """
    is_mm = flows["source"] == SourceFile.MUNI_MUNI.value
    touches_bcn = (flows["origin_zone_id"] == geo.BARCELONA) | (flows["dest_zone_id"] == geo.BARCELONA)
    days_with_barri_muni = flows.loc[flows["source"] == SourceFile.BARRI_MUNI.value, "day"].unique()
    drop = is_mm & touches_bcn & flows["day"].isin(days_with_barri_muni)
    return flows.loc[~drop].reset_index(drop=True)
