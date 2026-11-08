"""The web app's data file, built from the synthetic sample (no Telefónica data needed)."""

import json

import numpy as np
import pandas as pd

from prisa import export, reference, schemas


def _payload():
    fares, calendar, events, places = export.sample_inputs()
    return export.payload(fares, reference.load(schemas.FARE_PRODUCTS), calendar, events, places, illustrative=True)


def test_one_entry_per_day_with_discounts_in_range():
    data = _payload()
    days = data["days"]
    assert [d["d"] for d in days] == [d.strftime("%Y-%m-%d") for d in pd.date_range("2027-01-01", "2027-12-31")]
    disc = np.array([d["disc"] for d in days])
    assert disc.min() >= 0 and disc.max() <= data["meta"]["max_discount"]
    assert {d["lvl"] for d in days} <= set(range(5))


def test_level_and_discount_agree():
    # the curve gives no discount from pressure 0.6, which is where level "Alta" (3) starts
    for d in _payload()["days"]:
        assert (d["disc"] == 0) == (d["lvl"] >= 3)


def test_products_keep_published_prices_and_passes_are_flat():
    prods = {p["id"]: p for p in _payload()["products"]}
    assert prods["single"]["price"] == 2.90 and prods["single"]["daily"]
    assert prods["t_casual"]["per_trip"] and prods["t_casual"]["price"] == 1.30
    assert not prods["t_usual"]["daily"] and not prods["t_jove"]["daily"]


def test_places_have_a_level_for_every_day():
    data = _payload()
    assert [p["id"] for p in data["places"]] == [pid for pid, _, _ in export.PLACES]
    assert all(len(p["lvl"]) == len(data["days"]) for p in data["places"])


def test_events_and_holidays_reach_the_days():
    days = {d["d"]: d for d in _payload()["days"]}
    assert days["2027-06-24"]["hol"] == "Sant Joan"
    assert days["2027-06-03"]["ev"] == ["Evento de ejemplo"]
    assert export.event_label("FC Barcelona v Elche CF (LaLiga 2026-27)") == "FC Barcelona v Elche CF"
    assert export.event_label("Primavera Sound 2027") == "Primavera Sound"


def test_written_file_is_a_script_holding_the_json(tmp_path):
    path = export.write_js(_payload(), tmp_path / "sample.js")
    text = path.read_text(encoding="utf-8")
    assert "window.PRISA_DATA = " in text
    data = json.loads(text[text.index("{") : text.rindex("}") + 1])
    assert data["meta"]["illustrative"] is True
