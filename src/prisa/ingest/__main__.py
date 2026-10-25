"""Turn a folder of raw Telefónica files into our tables.

    uv run python -m prisa.ingest data/raw/telefonica --out data/processed

Every .csv / .parquet file under the folder is recognised by name (tourism)
or by its columns (mobility), loaded, checked against prisa.schemas and
written as one Parquet file per table. Files that match nothing are listed
and skipped, so a new file in the delivery never goes unnoticed.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from prisa import geo, schemas
from prisa.ingest import telefonica, tourism


def build(raw_dir: Path, out_dir: Path) -> dict[str, pd.DataFrame]:
    parts: dict[str, list[pd.DataFrame]] = defaultdict(list)
    skipped = []
    for path in sorted(p for p in Path(raw_dir).rglob("*") if p.suffix in (".csv", ".parquet")):
        if tourism.identify(path):
            table, df = tourism.load(path)
            parts[table.name].append(df)
        else:
            head = telefonica.read_head(path)
            source = telefonica.infer_source(list(head.columns), head)
            if source is None:
                skipped.append(path)
                continue
            parts[schemas.TRIP_FLOWS.name].append(telefonica.load_trip_flows(path, source))
        print(f"loaded {path.relative_to(raw_dir)}", file=sys.stderr)

    tables = {}
    for name, dfs in parts.items():
        df = pd.concat(dfs, ignore_index=True)
        if name == schemas.TRIP_FLOWS.name:
            df = telefonica.drop_barcelona_boundary_duplicates(df)
        tables[name] = schemas.check(df, schemas.TABLES[name])

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
    for p in skipped:
        print(f"skipped (not recognised): {p.relative_to(raw_dir)}", file=sys.stderr)
    return tables


def summary(tables: dict[str, pd.DataFrame]) -> str:
    lines = []
    for name, df in sorted(tables.items()):
        lines.append(f"{name}: {len(df):,} rows")
    if "trip_flows" in tables:
        f = tables["trip_flows"]
        barris = pd.concat([f["origin_zone_id"], f["dest_zone_id"]])
        barris = barris[barris.str.len() == 8]
        lines.append(f"trip_flows: {f['day'].min():%Y-%m-%d} to {f['day'].max():%Y-%m-%d}, "
                     f"{f['censored'].mean():.1%} censored, {barris.nunique()} barris")  # fmt: skip
        lines.append(f"barri numbering: {geo.infer_barri_numbering(barris)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("raw_dir", type=Path)
    p.add_argument("--out", type=Path, default=Path("data/processed"))
    args = p.parse_args(argv)
    print(summary(build(args.raw_dir, args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
