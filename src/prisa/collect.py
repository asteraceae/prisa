"""Archive feeds that only publish the present, so we build our own history.

    python -m prisa.collect fgc --out data/raw/collect   # every 2 minutes

Each run saves one gzipped snapshot, untouched, under
<out>/<source>/<YYYY-MM-DD>/<HHMMSS>Z.<ext>.gz. Parsing happens later
(`parse_fgc`), so a change in the feed never loses data.
Standard library only, so it runs on the server without the full project.

Sources and licences:
  fgc   FGC Geotren, live train positions with occupancy per car (CC BY 4.0)
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FEEDS = {
    "fgc": (
        "https://dadesobertes.fgc.cat/api/explore/v2.1/catalog/datasets/"
        "posicionament-dels-trens/records?limit=100",
        "json",
    ),
}
USER_AGENT = "prisa-collector/0.1 (UPF Mobile Data Challenge 2026)"


def fetch(url: str, attempts: int = 3, timeout: float = 30) -> bytes:
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(5 * (i + 1))
    raise AssertionError("unreachable")


def snapshot(source: str, out: Path, now: datetime | None = None) -> Path:
    url, ext = FEEDS[source]
    now = now or datetime.now(timezone.utc)
    body = fetch(url)
    if ext == "json":
        json.loads(body)  # refuse to archive an error page
    path = out / source / now.strftime("%Y-%m-%d") / f"{now.strftime('%H%M%S')}Z.{ext}.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(body))
    tmp.rename(path)
    return path


def _snapshot_time(path: Path) -> datetime:
    return datetime.strptime(f"{path.parent.name} {path.name[:6]}", "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)


def parse_fgc(path: Path) -> list[dict]:
    """Rows for `occupancy_observations` from one FGC snapshot (observed_at in UTC)."""
    observed_at = _snapshot_time(path)
    rows = []
    for r in json.loads(gzip.decompress(path.read_bytes()))["results"]:
        if not r.get("lin") or not r.get("ut"):
            continue  # the feed occasionally sends a record with its fields shifted (seen at night)
        try:
            cars = [float(c) for c in (r.get(f"ocupacio_{k}_percent") for k in ("mi", "ri", "m1", "m2"))
                    if c not in (None, "")]  # fmt: skip
        except (TypeError, ValueError):
            continue
        if not cars:
            continue
        next_stops = (r.get("properes_parades") or "").split(";")
        next_stop = json.loads(next_stops[0])["parada"] if next_stops[0] else None
        rows.append(
            {
                "observed_at": observed_at.replace(tzinfo=None),
                "line_id": f"fgc:{r['lin']}",
                "vehicle_id": r["ut"],
                "station_id": f"fgc:{r.get('estacionat_a') or next_stop}" if (r.get("estacionat_a") or next_stop) else None,
                "occupancy_pct": sum(cars) / len(cars),
                "source": "fgc_geotren",
            }
        )
    return rows



def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", choices=sorted(FEEDS))
    p.add_argument("--out", type=Path, default=Path("data/raw/collect"))
    args = p.parse_args(argv)
    try:
        print(snapshot(args.source, args.out))
    except Exception as e:  # one failed poll must not crash the timer loop; the log shows it
        print(f"{args.source}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
