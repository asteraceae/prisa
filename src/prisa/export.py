"""The fare calendar as data for the web app in frontend/.

    uv run python -m prisa.export            # the real 2027 calendar -> frontend/data/calendar.js (git-ignored)
    uv run python -m prisa.export --sample   # a synthetic calendar   -> frontend/data/sample.js (committed)

The app is static: it loads data/calendar.js when it exists and falls back to
data/sample.js, which says on screen that its numbers are illustrative. The
data is written as a .js file, not .json, so the app also works opened
straight from disk (browsers block fetch() on file://).

calendar.js is derived from the confidential Telefónica data: never commit it.

What the app shows follows the fare policy in prisa.fares: one discount per
day for the whole city, applied to the pay-per-trip titles; passes (T-usual,
T-jove) keep their price. Places only say how crowded each hotspot is
expected to be; they don't change the price.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from prisa import pressure, pricing, reference, schemas, store

FRONTEND_DATA = store.ROOT / "frontend" / "data"
DESIGN = "month"  # the design the team recommends (see prisa.pricing)
YEAR = 2027

# products the app lists, in order: (product_id, name, what it is)
PRODUCTS = [
    ("single", "Billete sencillo", "Un viaje"),
    ("t_casual", "T-casual", "10 viajes; precio por viaje"),
    ("t_dia", "T-dia", "Viajes ilimitados durante un día"),
    ("airport", "Billete aeropuerto", "Metro al aeropuerto"),
    ("t_usual", "T-usual", "Abono de 30 días para residentes"),
    ("t_jove", "T-jove", "Abono de 90 días, menores de 30"),
]
# hotspots the app shows: (id, label, official barri number)
PLACES = [
    ("sagrada_familia", "Sagrada Família", 6),
    ("park_guell", "Park Güell", 30),
    ("passeig_gracia", "Passeig de Gràcia", 7),
    ("gotic", "Barri Gòtic", 2),
    ("barceloneta", "Barceloneta", 3),
    ("montjuic", "Montjuïc", 11),
]


def level(p: pd.Series | np.ndarray) -> np.ndarray:
    """0-4 congestion level from pressure, on the same cut points as pressure.TIERS."""
    return np.searchsorted(pressure.TIER_EDGES, np.asarray(p, dtype=float), side="right")


def event_label(name: str) -> str:
    """'FC Barcelona v Elche CF (LaLiga 2026-27)' -> 'FC Barcelona v Elche CF'; drops trailing years."""
    name = re.sub(r"\s*\(.*?\)", "", name)
    return re.sub(rf"\s+{YEAR}$", "", name).strip()


def products_payload(products: pd.DataFrame) -> list[dict]:
    p = products.set_index("product_id")
    out = []
    for pid, name, what in PRODUCTS:
        r = p.loc[pid]
        trips = r["trips_included"]
        per_trip = bool(pd.notna(trips) and trips > 1)
        out.append({
            "id": pid, "name": name, "what": what,
            "price": round(float(r["price_eur"]) / (float(trips) if per_trip else 1), 2),
            "per_trip": per_trip, "daily": bool(r["daily_priced"]),
        })  # fmt: skip
    return out


def days_payload(fares: pd.DataFrame, calendar: pd.DataFrame, events: pd.DataFrame) -> list[dict]:
    """One entry per day: pressure, discount, level, and what explains them."""
    f = fares.assign(day=pd.to_datetime(fares["day"])).sort_values("day")
    cal = calendar.assign(day=pd.to_datetime(calendar["day"])).set_index("day")
    ev: dict[pd.Timestamp, list[str]] = {}
    for e in events.itertuples():
        for d in pd.date_range(e.start_day, e.end_day):
            ev.setdefault(d, []).append(event_label(e.name))
    out = []
    for r in f.itertuples():
        c = cal.loc[r.day] if r.day in cal.index else None
        hol = c["holiday_name"] if c is not None and bool(c["public_holiday"]) else None
        out.append({
            "d": r.day.strftime("%Y-%m-%d"),
            "p": round(float(r.expected_pressure), 3),
            "disc": round(float(r.discount), 3),
            "lvl": int(level([r.expected_pressure])[0]),
            "hol": None if hol is None or pd.isna(hol) else str(hol),
            "school": bool(c["school_holiday"]) if c is not None else False,
            "ev": ev.get(r.day, []),
        })  # fmt: skip
    return out


def place_levels(forecasts: pd.DataFrame, zone_ids: dict[str, str], days: pd.DatetimeIndex) -> dict[str, list[int]]:
    """Per place: expected visitors ranked within each month (same rule as the month design), as 0-4 levels."""
    out = {}
    for pid, zid in zone_ids.items():
        z = forecasts[forecasts["zone_id"] == zid][["day", "predicted"]]
        pr = pricing.pressure_from_series(z, ["predicted"], within="month").set_index("day")["expected_pressure"]
        out[pid] = level(pr.reindex(days).fillna(0.5)).tolist()
    return out


def payload(fares, products, calendar, events, places: dict[str, list[int]], illustrative: bool) -> dict:
    labels = {pid: name for pid, name, _ in PLACES}
    return {
        "meta": {
            "year": YEAR, "design": DESIGN, "illustrative": illustrative,
            "generated": pd.Timestamp.now().strftime("%Y-%m-%d"),
            "max_discount": pricing.MAX_DISCOUNT, "levels": list(pressure.TIERS),
        },
        "products": products_payload(products),
        "days": days_payload(fares, calendar, events),
        "places": [{"id": pid, "name": labels[pid], "lvl": lv} for pid, lv in places.items()],
    }  # fmt: skip


def write_js(data: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(f"// Generated by `python -m prisa.export`. Do not edit.\nwindow.PRISA_DATA = {body};\n", encoding="utf-8")
    return path


def build_real() -> Path:
    fid, _ = pricing.calendar_ids(DESIGN)
    fares = store.read("daily_fares")
    fares = fares[fares["calendar_id"] == fid]
    days = pd.DatetimeIndex(pd.to_datetime(fares["day"]).sort_values())
    cw = pd.read_csv(store.REFERENCE / "barri_crosswalk.csv", dtype={"zone_id": str})
    zone_of = cw.set_index("barri_number")["zone_id"].to_dict()
    runs = store.read("model_runs")
    run = runs[runs["target"] == "visitors"].sort_values("created_at")["run_id"].iloc[-1]
    fc = store.read("forecasts")
    fc = fc[fc["run_id"] == run].assign(day=lambda x: pd.to_datetime(x["day"]))
    places = place_levels(fc, {pid: zone_of[n] for pid, _, n in PLACES}, days)
    data = payload(fares, reference.load(schemas.FARE_PRODUCTS), store.read("calendar_days"), store.read("events"),
                   places, illustrative=False)  # fmt: skip
    return write_js(data, FRONTEND_DATA / "calendar.js")


def sample_inputs(seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, list[int]]]:
    """A made-up year with a plausible shape (quiet winter, busy summer, busier weekends): no real data in it."""
    from prisa import fares as fares_mod

    rng = np.random.default_rng(seed)
    days = pd.date_range(f"{YEAR}-01-01", f"{YEAR}-12-31")
    season = 0.5 - 0.3 * np.cos((days.day_of_year - 20) / 365 * 2 * np.pi)
    week = np.array([-0.04, -0.06, -0.03, 0.0, 0.08, 0.1, 0.04])[days.day_of_week]
    raw = pd.DataFrame({"day": days, "v": season + week + rng.normal(0, 0.07, len(days))})
    exp = pricing.pressure_from_series(raw, ["v"], within="month")
    fares = fares_mod.build_calendar(exp, f"sample-{YEAR}-{DESIGN}", pricing.MAX_DISCOUNT)
    calendar = pd.DataFrame({"day": days, "public_holiday": False, "holiday_name": pd.NA, "school_holiday": False})
    for d, name in [("01-01", "Any Nou"), ("01-06", "Reis"), ("05-01", "Festa del Treball"), ("06-24", "Sant Joan"),
                    ("09-11", "Diada"), ("09-24", "La Mercè"), ("12-25", "Nadal"), ("12-26", "Sant Esteve")]:  # fmt: skip
        m = calendar["day"] == pd.Timestamp(f"{YEAR}-{d}")
        calendar.loc[m, ["public_holiday", "holiday_name"]] = [True, name]
    events = pd.DataFrame({"name": ["Evento de ejemplo"], "start_day": [f"{YEAR}-06-02"], "end_day": [f"{YEAR}-06-05"]})
    shift = {"sagrada_familia": 0.05, "park_guell": 0.1, "passeig_gracia": 0.0, "gotic": 0.05, "barceloneta": 0.15, "montjuic": -0.05}
    places = {}
    for pid, s in shift.items():
        summer = s * np.clip(1 - np.abs(days.month - 7) / 3, 0, 1)
        v = pd.DataFrame({"day": days, "v": raw["v"] + summer + rng.normal(0, 0.05, len(days))})
        places[pid] = level(pricing.pressure_from_series(v, ["v"], within="month")["expected_pressure"]).tolist()
    return fares, calendar, events, places


def build_sample() -> Path:
    fares, calendar, events, places = sample_inputs()
    data = payload(fares, reference.load(schemas.FARE_PRODUCTS), calendar, events, places, illustrative=True)
    return write_js(data, FRONTEND_DATA / "sample.js")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", action="store_true", help="write the synthetic sample instead of the real calendar")
    args = ap.parse_args(argv)
    path = build_sample() if args.sample else build_real()
    print(f"wrote {path.relative_to(store.ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
