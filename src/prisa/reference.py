"""Load the hand-written CSVs in data/reference/ into their schema."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from prisa import schemas

REFERENCE_DIR = Path(__file__).resolve().parents[2] / "data" / "reference"


def load(table: schemas.Table, path: Path | None = None) -> pd.DataFrame:
    path = path or REFERENCE_DIR / f"{table.name}.csv"
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    out = {}
    for col in table.columns:
        s = raw[col.name] if col.name in raw else pd.Series("", index=raw.index)
        s = s.replace("", None)
        if col.dtype == schemas.DATE:
            out[col.name] = pd.to_datetime(s)
        elif col.dtype == schemas.BOOL:
            out[col.name] = s.str.lower().map({"true": True, "false": False}).astype("boolean")
        elif col.dtype in (schemas.INT, schemas.FLOAT):
            out[col.name] = pd.to_numeric(s).astype(col.dtype)
        else:
            out[col.name] = s.astype("string")
    return schemas.check(pd.DataFrame(out), table)
