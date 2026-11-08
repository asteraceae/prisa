"""Run the whole analysis in order.

    uv run python -m prisa.pipeline                 # analysis steps only
    uv run python -m prisa.pipeline --ingest        # also rebuild from the raw Telefónica delivery
    uv run python -m prisa.pipeline --external      # also re-run the external dataset builders

Steps: features (barri_day, city_day) -> saturation (transit_load) ->
pressure (components, index) -> forecast (2027) -> pricing (calendars) ->
simulate (scenarios) -> figures -> export (the web app's
data, frontend/data/calendar.js). Each step reads the previous step's tables
from data/processed/, so any step can also be run alone with
`uv run python -m prisa.<step>`.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path

EXTERNAL = ("geography", "calendar", "transit", "ridership", "mitma")
STEPS = ("features", "saturation", "pressure", "forecast", "pricing", "simulate", "figures", "export")


def _run(module: str, argv: list[str] | None = None) -> None:
    t = time.time()
    print(f"== {module}", flush=True)
    mod = importlib.import_module(module)
    if argv is not None:
        mod.main(argv)
    elif hasattr(mod, "main"):
        mod.main()
    else:
        mod.build()
    print(f"   ({time.time() - t:.0f}s)", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ingest", type=Path, nargs="?", const=Path("data/raw/telefonica"), default=None,
                   help="raw Telefónica folder to ingest first (default data/raw/telefonica)")  # fmt: skip
    p.add_argument("--external", action="store_true", help="re-run external dataset builders first")
    args = p.parse_args(argv)
    if args.ingest:
        _run("prisa.ingest.__main__", [str(args.ingest), "--out", "data/processed"])
    if args.external:
        for name in EXTERNAL:
            _run(f"prisa.external.{name}", [])
    for step in STEPS:
        _run(f"prisa.{step}", [] if step == "export" else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
