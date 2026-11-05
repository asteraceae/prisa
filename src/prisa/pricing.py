"""The 2027 fare calendar: forecasts -> expected city pressure -> daily discount.

    uv run python -m prisa.pricing

Three calendar designs, compared by `prisa.simulate`:

  year     city pressure (non-resident visitors T2 + trips inside the city M1),
           each day ranked within its year: discounts follow the seasons
           (January and August are cheap);
  month    the same pressure ranked within each month: every month has its
           own cheap and dear days, which gives visitors a reason to move a
           day within their stay rather than their whole trip;
  hotspots visitors in the barris where visitors concentrate (each barri
           weighted by its share of all visitor-days), ranked within the
           month: follows the monuments, not the commuters, so summer
           tourist peaks aren't discounted.

Ranking makes the share of discount days a policy choice instead of a side
effect of growth: with the default curve (full discount at or below pressure
0.2, none from 0.6) about a fifth of days get the full discount and two
fifths get none, matching the 30/20/10/0/0 % levels of the team's mockup.

The same function applied to observed 2023-2025 data gives the calendar we
*would* have run, which the simulation uses as a backtest.
"""

from __future__ import annotations

import sys

import pandas as pd

from prisa import fares, geo, pressure, reference, schemas, store

MAX_DISCOUNT = 0.30
CITY_SERIES = ("city_visitors", "city_trips_internal")


DESIGNS = ("year", "month", "hotspots")


def pressure_from_series(df: pd.DataFrame, cols: list[str], within: str = "year") -> pd.DataFrame:
    """day, expected_pressure: mean percentile of the given columns, ranked within each year or month."""
    day = pd.to_datetime(df["day"])
    group = day.dt.year if within == "year" else day.dt.to_period("M")
    pct = pd.concat([df.groupby(group)[c].rank(pct=True) for c in cols], axis=1).mean(axis=1)
    return pd.DataFrame({"day": day, "expected_pressure": pct.astype("float64")})


def _latest(forecasts: pd.DataFrame, model_runs: pd.DataFrame, target: str) -> pd.DataFrame:
    run = model_runs[model_runs["target"] == target].sort_values("created_at")["run_id"].iloc[-1]
    return forecasts[forecasts["run_id"] == run]


def hotspot_series(barri_visitors: pd.DataFrame, weights: pd.Series) -> pd.DataFrame:
    """day, hotspot_visitors: barri visitors summed with each barri's visitor-share weight."""
    w = barri_visitors["zone_id"].map(weights).fillna(0)
    s = (barri_visitors["value"] * w).groupby(barri_visitors["day"]).sum()
    return s.rename("hotspot_visitors").reset_index()


def expected_2027(forecasts: pd.DataFrame, model_runs: pd.DataFrame, design: str, weights: pd.Series) -> pd.DataFrame:
    """Expected pressure for each forecast day under one calendar design."""
    if design == "hotspots":
        f = _latest(forecasts, model_runs, "visitors").rename(columns={"predicted": "value"})
        return pressure_from_series(hotspot_series(f, weights), ["hotspot_visitors"], within="month")
    wide = {t: _latest(forecasts, model_runs, t).query("zone_id == @geo.BARCELONA").set_index("day")["predicted"]
            for t in CITY_SERIES}  # fmt: skip
    df = pd.DataFrame(wide).reset_index().rename(columns={"index": "day"})
    return pressure_from_series(df, list(CITY_SERIES), within=design)


def observed_pressure(design: str, weights: pd.Series) -> pd.DataFrame:
    """The same measure on actual 2023-2025 data (Jan-Aug for 2025): the calendar we would have run."""
    if design == "hotspots":
        bd = store.read("barri_day").rename(columns={"visitors": "value"})
        bd["day"] = pd.to_datetime(bd["day"])
        return pressure_from_series(hotspot_series(bd, weights), ["hotspot_visitors"], within="month")
    c = store.read("city_day").rename(columns={"visitors": "city_visitors", "trips_internal": "city_trips_internal"})
    return pressure_from_series(c, list(CITY_SERIES), within=design)


def calendar(pressure: pd.DataFrame, calendar_id: str, max_discount: float = MAX_DISCOUNT) -> pd.DataFrame:
    return fares.build_calendar(pressure, calendar_id, max_discount)


def monthly_summary(daily_fares: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Per month: days with a discount, mean discount, mean single-ticket price, days per level."""
    d = daily_fares.assign(month=pd.to_datetime(daily_fares["day"]).dt.month)
    single = prices[prices["product_id"] == "single"].assign(month=lambda x: pd.to_datetime(x["day"]).dt.month)
    out = d.groupby("month").agg(
        days=("day", "size"),
        discount_days=("discount", lambda s: int((s > 0).sum())),
        mean_discount=("discount", "mean"),
    )
    out["mean_single_eur"] = single.groupby("month")["price_today_eur"].mean()
    levels = d.pivot_table(index="month", columns="tier", values="day", aggfunc="size", fill_value=0)
    return out.join(levels).reset_index()


def calendar_ids(design: str) -> tuple[str, str]:
    return f"prisa-2027-{design}", f"backtest-{design}"


def build() -> dict[str, pd.DataFrame]:
    products = reference.load(schemas.FARE_PRODUCTS)
    forecasts, runs = store.read("forecasts"), store.read("model_runs")
    weights = pressure.visitor_weights(store.read("barri_day"))
    cals, meta, summaries, all_prices = [], [], [], []
    today = pd.Timestamp.now().normalize()
    for design in DESIGNS:
        fid, bid = calendar_ids(design)
        c27 = calendar(expected_2027(forecasts, runs, design, weights), fid)
        cals += [c27, calendar(observed_pressure(design, weights), bid)]
        prices = fares.product_prices(c27, products)
        if not fares.never_above_published(prices).empty:
            raise AssertionError("a product would cost more than its published price")
        if not fares.check_resident_fairness(prices, 22.80, 40, ["t_usual", "t_jove"]).empty:
            raise AssertionError("a resident title would cost more per trip than T-usual")
        all_prices.append(prices)
        summaries.append(monthly_summary(c27, prices).assign(calendar_id=fid))
        meta += [
            {"calendar_id": fid, "year": 2027, "forecast_run_id": design, "policy": "one_price_for_all",
             "max_discount": MAX_DISCOUNT, "created_at": today},
            {"calendar_id": bid, "year": 2023, "forecast_run_id": "observed", "policy": "one_price_for_all",
             "max_discount": MAX_DISCOUNT, "created_at": today},
        ]  # fmt: skip
    daily = pd.concat(cals, ignore_index=True)
    store.write(daily, "daily_fares")
    store.write(pd.concat(all_prices, ignore_index=True), "product_prices_2027")
    summary = pd.concat(summaries, ignore_index=True)
    store.write(summary, "fare_calendar_2027_monthly")
    meta = pd.DataFrame(meta).astype({"calendar_id": "string", "forecast_run_id": "string", "policy": "string", "year": "Int64"})
    store.write(meta, "fare_calendars")
    return {"daily_fares": daily, "summary": summary}


def main() -> int:
    out = build()
    cal = out["daily_fares"]
    for design in DESIGNS:
        c27 = cal[cal["calendar_id"] == calendar_ids(design)[0]]
        by_month = c27.groupby(pd.to_datetime(c27["day"]).dt.month)["discount"].mean()
        print(f"{design:9s} 2027: {int((c27['discount'] > 0).sum())} discount days, mean {c27['discount'].mean():.1%}; "
              f"mean discount by month: {' '.join(f'{v:.0%}' for v in by_month)}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
