"""Turn the raw Telefónica delivery into our tables.

    uv run python -m prisa.ingest data/raw/telefonica --out data/processed

Mobility CSVs go through DuckDB into a partitioned Parquet dataset,
data/processed/trip_flows/source=*/year=*/. The tourism files are small and
go through pandas into one Parquet file per table. Every table is checked
against prisa.schemas; files that match nothing are listed, so a new file in
a delivery never goes unnoticed. A short report is printed and saved next to
the output (it describes confidential data, so it stays out of git).
"""

from __future__ import annotations

import argparse
import glob
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from prisa import geo, schemas
from prisa.ingest import telefonica, tourism


def build_tourism(raw_dir: Path, out_dir: Path) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, list[str]]:
    parts: dict[str, list[pd.DataFrame]] = defaultdict(list)
    residuals, skipped = [], []
    for p in sorted(glob.glob(f"{raw_dir}/**/*", recursive=True)):
        path = Path(p)
        if path.suffix not in (".csv", ".parquet") or path.is_dir():
            continue
        if tourism.identify(path):
            table, df, residual = tourism.load(path)
            parts[table.name].append(df)
            residuals.append(residual)
        elif telefonica.identify(path) is None:
            skipped.append(str(path.relative_to(raw_dir)))

    tables = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, dfs in parts.items():
        df = schemas.check(pd.concat(dfs, ignore_index=True), schemas.TABLES[name])
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        tables[name] = df
    res = pd.DataFrame(residuals)
    if len(res):
        res = res.groupby("file", as_index=False).sum()
        res["share"] = res["visitors"] / res["total_visitors"]
        res.to_parquet(out_dir / "tourism_residuals.parquet", index=False)
    return tables, res, skipped


def report(processed: Path, tourism_tables: dict[str, pd.DataFrame], residuals: pd.DataFrame, skipped: list[str]) -> str:
    con = telefonica.connect()
    flows = telefonica.trip_flows_sql(processed)
    stats = telefonica.check_converted(processed, con)
    lines = ["# Ingest report", "", "## trip_flows", "", f"{stats['rows']:,} rows; all contract checks passed.", ""]
    lines += ["| source | rows | days | first | last | trips | censored rows |", "|---|---|---|---|---|---|---|"]
    for r in con.execute(
        f"SELECT source, count(*), count(DISTINCT day), min(day), max(day), sum(trips), avg(censored::INT) "
        f"FROM {flows} GROUP BY 1 ORDER BY 1"
    ).fetchall():
        lines.append(f"| {r[0]} | {r[1]:,} | {r[2]} | {r[3]} | {r[4]} | {r[5]:,} | {r[6]:.1%} |")
    barris = pd.Series(
        [r[0] for r in con.execute(
            f"SELECT origin_zone_id z FROM {flows} UNION SELECT dest_zone_id FROM {flows}"
        ).fetchall()]
    )  # fmt: skip
    barris = barris[barris.str.len() == geo.BARRI_LEN]
    lines += ["", f"{len(barris)} barris; numbering: {geo.infer_barri_numbering(barris)}", ""]

    lines += ["## Tourism tables", "", "| table | rows |", "|---|---|"]
    lines += [f"| {n} | {len(df):,} |" for n, df in sorted(tourism_tables.items())]
    if len(residuals):
        lines += ["", "Remainder rows (groups < 15 pooled as NA in a key column), dropped:", "",
                  "| file | rows | visitors | share of file's visitors |", "|---|---|---|---|"]  # fmt: skip
        for r in residuals.itertuples():
            lines.append(f"| {r.file} | {r.rows:,} | {r.visitors:,} | {r.share:.2%} |")
    if skipped:
        lines += ["", "Not recognised (skipped):", ""] + [f"- {s}" for s in skipped]
    return "\n".join(lines) + "\n"


def build(raw_dir: Path, out_dir: Path) -> str:
    telefonica.convert(raw_dir, out_dir)
    tables, residuals, skipped = build_tourism(raw_dir, out_dir)
    text = report(out_dir, tables, residuals, skipped)
    (out_dir / "ingest_report.md").write_text(text)
    return text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("raw_dir", type=Path)
    p.add_argument("--out", type=Path, default=Path("data/processed"))
    args = p.parse_args(argv)
    print(build(args.raw_dir, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
