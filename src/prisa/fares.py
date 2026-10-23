"""Daily fare calendar and the resident fairness check.

Policy (team decision, 23 Oct 2026):
  * one discount per day, applied to every product and every traveller;
  * the fare only ever goes down from the published price, never up;
  * residents must never pay more than the reference fare (T-usual).

Alternative policy kept for comparison: "registered_residents_capped", where a
title tied to a DNI/NIE (T-mobilitat registration) shields residents and
Spanish citizens from any day's price and only unregistered travellers see
the daily price.
"""

from __future__ import annotations

import pandas as pd

from prisa import schemas

POLICIES = ("one_price_for_all", "registered_residents_capped")


def discount_curve(expected_pressure: pd.Series, max_discount: float, quiet: float = 0.2, busy: float = 0.6) -> pd.Series:
    """Map expected pressure (0-1) to a continuous discount (0-max_discount).

    Pressure at or below `quiet` gets the full discount, at or above `busy`
    gets none, linear in between. Tiers for display are cut from this later.
    """
    if not 0 <= max_discount < 1:
        raise ValueError("max_discount must be in [0, 1)")
    t = ((busy - expected_pressure) / (busy - quiet)).clip(0, 1)
    return (t * max_discount).astype(float)


def build_calendar(expected_pressure: pd.DataFrame, calendar_id: str, max_discount: float, **curve) -> pd.DataFrame:
    """`expected_pressure` has columns day, expected_pressure (city level, 0-1)."""
    out = expected_pressure[["day", "expected_pressure"]].copy()
    out["discount"] = discount_curve(out["expected_pressure"], max_discount, **curve)
    out.insert(0, "calendar_id", pd.Series(calendar_id, index=out.index, dtype="string"))
    return schemas.check(out[schemas.DAILY_FARES.column_names], schemas.DAILY_FARES)


def product_prices(daily_fares: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    """Every product's price on every day.

    Daily-priced products cost base price x (1 - discount). Flat passes
    (T-usual, T-jove) keep their published price: their holders pay nothing
    per trip, so a daily price can't reach them. T-usual is about 70% of
    T-mobilitat validations (ATM, March 2024), so the discount only steers the
    remaining pay-per-trip travellers, who are mostly visitors.
    """
    cols = ["product_id", "price_eur", "kind", "trips_included", "validity_days", "daily_priced"]
    df = daily_fares.merge(products[cols], how="cross")
    discount = df["discount"].where(df["daily_priced"].astype(bool), 0.0)
    df["price_today_eur"] = (df["price_eur"] * (1 - discount)).round(2)
    return df


def never_above_published(prices: pd.DataFrame) -> pd.DataFrame:
    """Rows where a product costs more than its published price (empty = rule holds)."""
    return prices[prices["price_today_eur"] > prices["price_eur"] + 1e-9]


def per_trip_cost(price_eur: float, trips: float) -> float:
    return price_eur / trips


def check_resident_fairness(
    prices: pd.DataFrame,
    reference_price_eur: float,
    trips_per_30_days: float,
    resident_products: list[str],
) -> pd.DataFrame:
    """Days on which a resident product costs more per trip than the reference.

    T-usual is a 30-day unlimited pass, so it is compared per trip: its price
    divided by an assumed number of trips a resident makes in 30 days
    (`trips_per_30_days`). Returns the offending rows; empty = fair.
    """
    cap = per_trip_cost(reference_price_eur, trips_per_30_days)
    df = prices[prices["product_id"].isin(resident_products)].copy()
    # unlimited passes (trips_included null) are spread over the trips made while valid
    pass_trips = trips_per_30_days * pd.to_numeric(df["validity_days"], errors="coerce") / 30
    trips = pd.to_numeric(df["trips_included"], errors="coerce").fillna(pass_trips)
    df["per_trip_eur"] = df["price_today_eur"] / trips
    return df[df["per_trip_eur"] > cap + 1e-9]
