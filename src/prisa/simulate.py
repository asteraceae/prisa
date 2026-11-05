"""What a fare calendar might do: shift flexible visitors from dear days to cheap ones.

    uv run python -m prisa.simulate

We can't observe how visitors react to a daily price, so the response is a
scenario (A conservative, B central, C optimistic: the A/B/C in the team's
calendar mockup). Within each week, the flexible part of each segment's
public-transport demand moves away from days dearer than the week's average
and towards cheaper ones:

    shift(d) = -elasticity x relative_price(d) x flexible(d)
    relative_price(d) = price(d) / mean price of its week - 1
    flexible(d) = sum over segments: visitors(d, seg) x PT share(seg) x flexible share(seg)

Shifts are rebalanced so each week's total is unchanged: people move their
day, they don't appear or vanish. Residents' flexible share is 0 (T-usual is
flat). Revenue uses a mean pay-per-trip fare and trips per visitor-day, both
stated assumptions below.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

from prisa import pricing, reference, schemas, store
from prisa.enums import Segment, TransportMode

PT_MODES = {m.value for m in TransportMode}
# Mean fare of a visitor's pay-per-trip ride: 2016 survey mix of tourists' tickets
# (62.7% T-10 -> now T-casual 1.30, 13.3% single 2.90, rest Hola BCN / T-dia ~1.50).
FARE_PER_TRIP_EUR = 0.627 * 1.30 + 0.133 * 2.90 + 0.24 * 1.50
TRIPS_PER_VISITOR_DAY = 2.0  # assumption: a visitor on transport takes ~2 rides a day


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    elasticity: float
    flexible: dict[Segment, float]


SCENARIOS = (
    Scenario("A", "conservative", 0.2, {Segment.INTERNATIONAL: 0.15, Segment.NATIONAL: 0.10, Segment.REGIONAL: 0.05}),
    Scenario("B", "central", 0.5, {Segment.INTERNATIONAL: 0.30, Segment.NATIONAL: 0.20, Segment.REGIONAL: 0.10}),
    Scenario("C", "optimistic", 1.0, {Segment.INTERNATIONAL: 0.50, Segment.NATIONAL: 0.35, Segment.REGIONAL: 0.15}),
)


def pt_share() -> dict[str, float]:
    ms = reference.load(schemas.MODE_SHARE)
    return ms[ms["mode"].isin(PT_MODES)].groupby("segment")["share"].sum().to_dict()


def segment_visitors(days: pd.Series, city_day: pd.DataFrame, totals: pd.Series | None = None) -> pd.DataFrame:
    """Visitors per segment per day. For future days, the observed same-month segment mix scales `totals`."""
    seg_cols = {s.value: f"visitors_{s.value}" for s in Segment if s != Segment.RESIDENT}
    if totals is None:
        return city_day.set_index("day").loc[days, list(seg_cols.values())].rename(columns={v: k for k, v in seg_cols.items()})
    c = city_day.assign(month=pd.to_datetime(city_day["day"]).dt.month)
    mix = c.groupby("month")[list(seg_cols.values())].sum()
    mix = mix.div(mix.sum(axis=1), axis=0)
    m = pd.to_datetime(days).dt.month.to_numpy()
    out = mix.loc[m].reset_index(drop=True).mul(totals.to_numpy(), axis=0)
    out.index = pd.to_datetime(days).to_numpy()
    return out.rename(columns={v: k for k, v in seg_cols.items()})


def simulate(calendar: pd.DataFrame, visitors: pd.DataFrame, scenario: Scenario) -> pd.DataFrame:
    """Per day: base and simulated PT visitor rides, rides shifted, revenue change."""
    share = pt_share()
    d = calendar.set_index("day")[["discount", "expected_pressure"]].copy()
    d.index = pd.to_datetime(d.index)
    v = visitors.reindex(d.index)
    segs = [Segment(c) for c in v.columns]
    pt = sum(v[s] * share.get(s.value, 0) for s in segs) * TRIPS_PER_VISITOR_DAY
    flex = sum(v[s] * share.get(s.value, 0) * scenario.flexible.get(s, 0) for s in segs) * TRIPS_PER_VISITOR_DAY
    price = 1 - d["discount"]
    week = d.index.to_period("W")
    rel = price / price.groupby(week).transform("mean") - 1
    shift = -scenario.elasticity * rel * flex
    shift = shift - shift.groupby(week).transform("mean")  # keep each week's total
    sim = pt + shift
    revenue_base = pt * FARE_PER_TRIP_EUR
    revenue_sim = sim * FARE_PER_TRIP_EUR * price
    return pd.DataFrame({
        "day": d.index, "scenario_id": scenario.scenario_id, "expected_pressure": d["expected_pressure"].to_numpy(),
        "discount": d["discount"].to_numpy(), "pt_rides_base": pt.to_numpy(), "pt_rides_sim": sim.to_numpy(),
        "rides_shifted": shift.to_numpy(), "revenue_delta_eur": (revenue_sim - revenue_base).to_numpy(),
    })  # fmt: skip


def effect_summary(sim: pd.DataFrame) -> dict[str, float]:
    """Headline effects: how the busiest days change, and what it costs."""
    base, new = sim["pt_rides_base"], sim["pt_rides_sim"]
    busy = base >= base.quantile(0.8)
    thr = base.quantile(0.9)
    return {
        "busiest_fifth_change": float(new[busy].sum() / base[busy].sum() - 1),
        "days_over_base_p90": int((new > thr).sum()),
        "days_over_base_p90_before": int((base > thr).sum()),
        "peak_to_mean_before": float(base.max() / base.mean()),
        "peak_to_mean_after": float(new.max() / new.mean()),
        "cv_before": float(base.std() / base.mean()),
        "cv_after": float(new.std() / new.mean()),
        "rides_shifted_per_year": float(sim["rides_shifted"].clip(lower=0).sum()),
        "revenue_delta_eur": float(sim["revenue_delta_eur"].sum()),
        "revenue_base_eur": float((base * FARE_PER_TRIP_EUR).sum()),
    }


def build() -> dict[str, pd.DataFrame]:
    fares_ = store.read("daily_fares")
    city = store.read("city_day")
    city["day"] = pd.to_datetime(city["day"])
    fc = store.read("forecasts")
    runs = store.read("model_runs")
    run = runs[runs["target"] == "city_visitors"].sort_values("created_at")["run_id"].iloc[-1]
    total_2027 = fc[(fc["run_id"] == run)].set_index("day")["predicted"]

    out, summaries = [], []
    plans = []
    for design in pricing.DESIGNS:
        fid, bid = pricing.calendar_ids(design)
        plans += [(fid, total_2027), (bid, None)]
    for cal_id, totals in plans:
        cal = fares_[fares_["calendar_id"] == cal_id].copy()
        cal["day"] = pd.to_datetime(cal["day"])
        vis = segment_visitors(cal["day"], city, None if totals is None else totals.reindex(cal["day"]))
        for sc in SCENARIOS:
            sim = simulate(cal, vis, sc).assign(calendar_id=cal_id)
            out.append(sim)
            summaries.append({"calendar_id": cal_id, "scenario_id": sc.scenario_id, "name": sc.name,
                              "elasticity": sc.elasticity, **effect_summary(sim)})  # fmt: skip
    sims = pd.concat(out, ignore_index=True)
    summary = pd.DataFrame(summaries)
    store.write(sims, "simulation_daily")
    store.write(summary, "simulation_summary")
    scen = pd.DataFrame([
        {"scenario_id": s.scenario_id, "name": s.name, "elasticity": s.elasticity,
         **{f"flexible_share_{seg.value}": s.flexible.get(seg, 0.0) for seg in Segment}}
        for s in SCENARIOS
    ]).astype({"scenario_id": "string", "name": "string"})  # fmt: skip
    store.write(scen, "scenarios")
    return {"daily": sims, "summary": summary}


def main() -> int:
    s = build()["summary"]
    cols = ["calendar_id", "scenario_id", "busiest_fifth_change", "days_over_base_p90_before", "days_over_base_p90",
            "cv_before", "cv_after", "rides_shifted_per_year", "revenue_delta_eur", "revenue_base_eur"]  # fmt: skip
    print(s[cols].round(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
