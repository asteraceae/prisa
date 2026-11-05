"""Where processed tables live and how to open them.

    con = store.connect()                      # DuckDB with a view per table
    con.execute("SELECT * FROM barri_day LIMIT 5").df()
    df = store.read("barri_day")               # pandas, dtypes as in prisa.schemas

Every data/processed/<name>.parquet becomes a view <name>; the partitioned
trip_flows/ dataset becomes the view trip_flows.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from prisa import schemas

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
REFERENCE = ROOT / "data" / "reference"
OUTPUTS = ROOT / "outputs"


def connect(processed: Path = PROCESSED, memory_limit: str = "16GB") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}'")
    flows = processed / "trip_flows"
    if flows.exists():
        con.execute(
            f"CREATE VIEW trip_flows AS SELECT * FROM read_parquet('{flows}/**/*.parquet', hive_partitioning = true)"
        )
    for path in sorted(processed.glob("*.parquet")):
        con.execute(f"CREATE VIEW \"{path.stem}\" AS SELECT * FROM read_parquet('{path}')")
    return con


def read(name: str, processed: Path = PROCESSED) -> pd.DataFrame:
    """A processed table as pandas, cast to its contract's dtypes when it has one."""
    df = pd.read_parquet(processed / f"{name}.parquet")
    table = schemas.TABLES.get(name)
    if table is not None:
        df = df.astype({c.name: c.dtype for c in table.columns if c.name in df.columns and c.dtype != schemas.DATE})
    return df


def write(df: pd.DataFrame, name: str, processed: Path = PROCESSED) -> Path:
    """Cast to the contract's dtypes, check (if there is a contract) and write data/processed/<name>.parquet.

    The cast matters because tables read back from Parquet can come in as
    pyarrow-backed dtypes (double[pyarrow]) that the contract doesn't accept.
    """
    if name in schemas.TABLES:
        table = schemas.TABLES[name]
        casts = {c.name: c.dtype for c in table.columns if c.name in df.columns and c.dtype != schemas.DATE}
        df = schemas.check(df.astype(casts), table)
    processed.mkdir(parents=True, exist_ok=True)
    path = processed / f"{name}.parquet"
    df.to_parquet(path, index=False)
    return path
