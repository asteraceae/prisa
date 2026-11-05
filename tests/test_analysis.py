"""Analysis steps on small synthetic data (no Telefónica data needed)."""

import numpy as np
import pandas as pd
import pytest

from prisa import forecast, pressure, pricing, saturation, simulate
from prisa.enums import Segment


def test_tiers_follow_the_mockup_levels():
    t = pressure.tier(pd.Series([0.05, 0.25, 0.5, 0.7, 0.95]))
    assert t.tolist() == list(pressure.TIERS)


def test_rank_against_history_uses_each_zones_own_distribution():
    hist = pd.DataFrame({"component": "c", "zone_id": ["a"] * 4 + ["b"] * 4, "raw": [1, 2, 3, 4, 10, 20, 30, 40]})
    new = pd.DataFrame({"component": "c", "zone_id": ["a", "b"], "raw": [3, 3]})
    assert pressure.rank_against_history(hist, new).tolist() == [0.75, 0.0]


def test_pressure_ranked_within_month_gives_every_month_cheap_days():
    days = pd.date_range("2027-01-01", "2027-12-31")
    seasonal = pd.DataFrame({"day": days, "v": 100 + 50 * (days.month == 8)})  # August uniformly busier
    seasonal["v"] = seasonal["v"] + np.arange(len(days)) % 7  # plus a weekly ripple
    by_year = pricing.pressure_from_series(seasonal, ["v"], within="year")
    by_month = pricing.pressure_from_series(seasonal, ["v"], within="month")
    aug = by_year["day"].dt.month == 8
    assert by_year.loc[aug, "expected_pressure"].min() > 0.8  # whole of August dear
    assert by_month.loc[aug, "expected_pressure"].min() < 0.2  # August still has cheap days


def test_simulation_moves_rides_from_dear_to_cheap_days_and_keeps_weekly_totals():
    days = pd.date_range("2027-03-01", periods=14)  # two full weeks, Monday start
    cal = pd.DataFrame({"day": days, "discount": [0.3, 0.3, 0, 0, 0, 0, 0] * 2, "expected_pressure": 0.5})
    vis = pd.DataFrame({"international": 1000.0, "national": 500.0, "regional": 800.0}, index=days)
    sim = simulate.simulate(cal, vis, simulate.SCENARIOS[1])
    weekly = sim.groupby(sim["day"].dt.to_period("W"))[["pt_rides_base", "pt_rides_sim"]].sum()
    assert np.allclose(weekly["pt_rides_base"], weekly["pt_rides_sim"])
    assert (sim.loc[sim["discount"] > 0, "rides_shifted"] > 0).all()
    assert (sim.loc[sim["discount"] == 0, "rides_shifted"] < 0).all()


def test_day_types_match_gtfs_service_days():
    cal = pd.DataFrame({"day": pd.to_datetime(["2027-05-01", "2027-05-03", "2027-05-08", "2027-05-09"]),
                        "public_holiday": [True, False, False, False]})  # fmt: skip
    got = saturation.day_types(cal, pd.Series(cal["day"]))
    assert got.tolist() == ["sunday_holiday", "weekday", "saturday", "sunday_holiday"]


def test_calendar_features_know_easter_and_holidays():
    f = forecast.calendar_features(pd.date_range("2027-03-26", "2027-03-29"))
    assert f["easter_offset"].tolist() == [-2, -1, 0, 1]
    assert f.loc["2027-03-29", "public_holiday"] == 1  # Easter Monday (Catalonia)


def test_pattern_model_recovers_a_weekly_shape():
    days = pd.date_range("2023-01-01", "2024-12-31")
    rng = np.random.default_rng(0)
    shape = np.array([1.0, 1.0, 1.0, 1.05, 1.2, 1.1, 0.8])
    rows = [pd.DataFrame({"day": days, "zone_id": z, "y": base * shape[days.day_of_week] * rng.lognormal(0, 0.02, len(days))})
            for z, base in (("a", 1000), ("b", 5000))]  # fmt: skip
    df = pd.concat(rows, ignore_index=True)
    fitted = forecast.fit_pattern(df, forecast._full_year_levels(df))
    week = pd.DataFrame({"day": pd.date_range("2025-03-03", periods=7), "zone_id": "a"})
    pat = np.exp(fitted.pattern(week))
    assert pd.Series(pat).idxmax() == 4 and pd.Series(pat).idxmin() == 6  # Friday busiest, Sunday quietest
    assert pat[4] / pat[6] == pytest.approx(1.2 / 0.8, rel=0.1)
