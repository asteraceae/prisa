"""Forecast how busy each day of 2027 will be, per barri and for the whole city.

    uv run python -m prisa.forecast

The fare calendar only needs to know which days will be busier than others,
not the absolute 2027 numbers, so each series is split in two:

    log(1 + y) = level(zone, year) + pattern(zone, calendar)

`level` is the zone's yearly mean (estimated for partial years from the
pattern); `pattern` is learnt by one gradient-boosting model across all zones
from calendar features (weekday, season, holidays, Easter, school holidays,
events). Growth from year to year only moves `level`, which a tree model
couldn't extrapolate anyway; 2027 levels are held at the latest estimate
(an explicit, documented assumption).

Evaluation is honest: fit on 2023-2024, predict Jan-Aug 2025 unseen, and
compare with a seasonal baseline. What matters for pricing is getting the
ranking of days right, so ranking metrics are reported next to error.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from dateutil.easter import easter
from sklearn.ensemble import HistGradientBoostingRegressor

from prisa import geo, schemas, store

TRAIN_END = pd.Timestamp("2024-12-31")
TEST_START = pd.Timestamp("2025-01-01")
HORIZON = (pd.Timestamp("2027-01-01"), pd.Timestamp("2027-12-31"))

# (table, column) per target; city targets use zone_id = 08019
TARGETS = {
    "visitors": ("barri_day", "visitors"),
    "trips_in": ("barri_day", "trips_in"),
    "city_visitors": ("city_day", "visitors"),
    "city_trips_internal": ("city_day", "trips_internal"),
}


# --- calendar features --------------------------------------------------------


def _fallback_calendar(days: pd.DatetimeIndex) -> pd.DataFrame:
    """Holidays from the `holidays` library plus Barcelona's local days; no school holidays."""
    import holidays

    years = sorted(set(days.year))
    hol = holidays.Spain(subdiv="CT", years=years)
    for y in years:  # Barcelona local: Pentecost Monday (Segona Pasqua) and La Mercè
        hol[pd.Timestamp(easter(y)) + pd.Timedelta(days=50)] = "Segona Pasqua"
        hol[date(y, 9, 24)] = "La Mercè"
    df = pd.DataFrame(index=days)
    df["public_holiday"] = [d.date() in hol for d in days]
    df["school_holiday"] = False
    return df


def calendar_features(days: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per day: everything the model knows about a date in advance."""
    days = pd.DatetimeIndex(days).normalize()
    cal_path = store.PROCESSED / "calendar_days.parquet"
    if cal_path.exists():
        cal = pd.read_parquet(cal_path).assign(day=lambda d: pd.to_datetime(d["day"])).set_index("day")
        base = cal.reindex(days)[["public_holiday", "school_holiday", "is_bridge_day"]].astype(float)
    else:
        base = _fallback_calendar(days).astype(float)
        base["is_bridge_day"] = 0.0
    f = pd.DataFrame(index=days)
    f["dow"] = days.day_of_week
    f["month"] = days.month
    doy = days.day_of_year.to_numpy()
    f["doy_sin"], f["doy_cos"] = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
    f["week"] = days.isocalendar().week.to_numpy().astype(int)
    f["public_holiday"] = base["public_holiday"].fillna(0).to_numpy()
    f["school_holiday"] = base["school_holiday"].fillna(0).to_numpy()
    f["bridge_day"] = base["is_bridge_day"].fillna(0).to_numpy()
    hol = pd.Series(f["public_holiday"].to_numpy(), index=days)
    f["holiday_eve"] = hol.shift(-1, freq="D").reindex(days).fillna(0).to_numpy()
    f["after_holiday"] = hol.shift(1, freq="D").reindex(days).fillna(0).to_numpy()
    # days from Easter Sunday, clipped: Holy Week moves between March and April
    e = pd.to_datetime([easter(y) for y in days.year])
    f["easter_offset"] = np.clip((days - e).days, -10, 10)
    f["events"] = _event_counts(days)
    return f


def _event_counts(days: pd.DatetimeIndex) -> np.ndarray:
    path = store.PROCESSED / "events.parquet"
    if not path.exists():
        return np.zeros(len(days))
    ev = pd.read_parquet(path)
    counts = pd.Series(0.0, index=days)
    for r in ev.itertuples():
        span = pd.date_range(pd.Timestamp(r.start_day), pd.Timestamp(r.end_day))
        counts[counts.index.isin(span)] += 1
    return counts.to_numpy()


# --- data -----------------------------------------------------------------------


def series(target: str) -> pd.DataFrame:
    """Long frame day, zone_id, y for one target."""
    table, col = TARGETS[target]
    df = store.read(table)
    if table == "city_day":
        df = df.assign(zone_id=geo.BARCELONA)
    out = df[["day", "zone_id", col]].rename(columns={col: "y"}).dropna()
    out["day"] = pd.to_datetime(out["day"])
    return out


def design(df: pd.DataFrame) -> pd.DataFrame:
    cal = calendar_features(pd.DatetimeIndex(df["day"].unique()))
    X = cal.loc[df["day"]].reset_index(drop=True)
    X["zone"] = pd.Categorical(df["zone_id"].to_numpy()).codes
    return X


def _full_year_levels(df: pd.DataFrame) -> pd.Series:
    """Mean log(1+y) per zone and complete calendar year."""
    d = df.assign(year=df["day"].dt.year, ly=np.log1p(df["y"]))
    full = d.groupby("year")["day"].transform("nunique") >= 365
    return d[full].groupby(["zone_id", "year"])["ly"].mean()


# --- model ----------------------------------------------------------------------


@dataclass
class Fitted:
    model: HistGradientBoostingRegressor
    zones: pd.Index  # category order used for the `zone` feature

    def pattern(self, df: pd.DataFrame) -> np.ndarray:
        X = design(df)
        X["zone"] = pd.Categorical(df["zone_id"].to_numpy(), categories=self.zones).codes
        return self.model.predict(X)


def fit_pattern(df: pd.DataFrame, levels: pd.Series) -> Fitted:
    d = df.assign(year=df["day"].dt.year)
    lv = levels.rename("level").reset_index()
    d = d.merge(lv, on=["zone_id", "year"])
    zones = pd.Index(sorted(d["zone_id"].unique()))
    X = design(d)
    X["zone"] = pd.Categorical(d["zone_id"].to_numpy(), categories=zones).codes
    y = np.log1p(d["y"].to_numpy()) - d["level"].to_numpy()
    cat = [X.columns.get_loc(c) for c in ("zone", "dow", "month")]
    model = HistGradientBoostingRegressor(
        max_iter=600, learning_rate=0.05, max_leaf_nodes=63, l2_regularization=1.0,
        categorical_features=cat, random_state=0,
    )  # fmt: skip
    model.fit(X, y)
    return Fitted(model, zones)


def estimate_levels(fitted: Fitted, df: pd.DataFrame) -> pd.Series:
    """Level per zone-year for any span (also partial years): mean of log(1+y) - pattern."""
    d = df.assign(year=df["day"].dt.year, resid=np.log1p(df["y"]) - fitted.pattern(df))
    return d.groupby(["zone_id", "year"])["resid"].mean()


def seasonal_baseline(train: pd.DataFrame, test: pd.DataFrame, levels: pd.Series) -> np.ndarray:
    """Mean pattern per zone x month x weekday in training: the obvious thing to beat."""
    t = train.assign(year=train["day"].dt.year).merge(levels.rename("level").reset_index(), on=["zone_id", "year"])
    t["rel"] = np.log1p(t["y"]) - t["level"]
    key = lambda d: [d["zone_id"], d["day"].dt.month, d["day"].dt.day_of_week]  # noqa: E731
    prof = t.groupby(key(t))["rel"].mean()
    idx = pd.MultiIndex.from_arrays(key(test))
    return prof.reindex(idx).to_numpy()


def _rank_metrics(df: pd.DataFrame, pred_col: str) -> dict[str, float]:
    """Within each zone: Spearman of predicted vs actual, and hit rates on busiest / quietest fifth of days."""
    rows = []
    for _, g in df.groupby("zone_id"):
        if len(g) < 20:
            continue
        a, p = g["y"].rank(pct=True), g[pred_col].rank(pct=True)
        rows.append({
            "spearman": a.corr(p),
            "top_hit": ((a > 0.8) & (p > 0.8)).sum() / max((a > 0.8).sum(), 1),
            "bottom_hit": ((a <= 0.2) & (p <= 0.2)).sum() / max((a <= 0.2).sum(), 1),
        })  # fmt: skip
    return pd.DataFrame(rows).median().to_dict()


def evaluate(target: str) -> dict:
    """Fit on 2023-2024, predict Jan-Aug 2025, compare with the seasonal baseline."""
    df = series(target)
    train, test = df[df["day"] <= TRAIN_END], df[df["day"] >= TEST_START].copy()
    levels = _full_year_levels(train)
    fitted = fit_pattern(train, levels)
    test["pattern"] = fitted.pattern(test)
    test["baseline"] = seasonal_baseline(train, test, levels)
    # oracle level for 2025 (from actuals) isolates how well the *shape* is predicted
    lv = (np.log1p(test["y"]) - test["pattern"]).groupby(test["zone_id"]).transform("mean")
    lv_b = (np.log1p(test["y"]) - test["baseline"]).groupby(test["zone_id"]).transform("mean")
    test["pred"] = np.expm1(test["pattern"] + lv)
    test["pred_baseline"] = np.expm1(test["baseline"] + lv_b)
    ape = lambda col: (test[col] - test["y"]).abs() / test["y"].clip(lower=1)  # noqa: E731
    return {
        "target": target,
        "n_test": int(len(test)),
        "model": {"mape": float(ape("pred").median()), **_rank_metrics(test, "pred")},
        "baseline": {"mape": float(ape("pred_baseline").median()), **_rank_metrics(test, "pred_baseline")},
        "test_frame": test,
    }


def forecast(target: str, start: pd.Timestamp = HORIZON[0], end: pd.Timestamp = HORIZON[1]) -> tuple[pd.DataFrame, dict]:
    """Refit on all data and forecast every zone for start..end. Returns (forecasts rows, model_run row)."""
    df = series(target)
    levels = _full_year_levels(df)
    fitted = fit_pattern(df[df["day"] <= TRAIN_END], levels)
    all_levels = estimate_levels(fitted, df)  # adds the partial 2025 level
    fitted = fit_pattern(df, all_levels)
    resid = np.log1p(df["y"]) - fitted.pattern(df) - all_levels.reindex(
        pd.MultiIndex.from_arrays([df["zone_id"], df["day"].dt.year])
    ).to_numpy()
    lo, hi = np.quantile(resid, [0.1, 0.9])
    last = all_levels.groupby(level="zone_id").last()  # latest level, held flat (assumption)

    days = pd.date_range(start, end)
    grid = pd.MultiIndex.from_product([days, fitted.zones], names=["day", "zone_id"]).to_frame(index=False)
    pat = fitted.pattern(grid)
    lvl = grid["zone_id"].map(last).to_numpy()
    run_id = f"{target}-{pd.Timestamp.now():%Y%m%d%H%M}"
    out = pd.DataFrame({
        "run_id": run_id, "day": grid["day"], "zone_id": grid["zone_id"],
        "predicted": np.expm1(pat + lvl), "lower": np.expm1(pat + lvl + lo), "upper": np.expm1(pat + lvl + hi),
    })  # fmt: skip
    out = out.astype({"run_id": "string", "zone_id": "string"})
    ev = evaluate(target)
    run = {
        "run_id": run_id, "created_at": pd.Timestamp.now().normalize(), "target": target,
        "train_from": df["day"].min(), "train_to": df["day"].max(),
        "features": json.dumps(list(design(df.head(1)).columns)),
        "metrics": json.dumps({k: ev[k] for k in ("n_test", "model", "baseline")}),
    }  # fmt: skip
    return schemas.check(out, schemas.FORECASTS), run


def main() -> int:
    runs, outs = [], []
    for target in TARGETS:
        out, run = forecast(target)
        outs.append(out)
        runs.append(run)
        m = json.loads(run["metrics"])
        print(f"{target}: 2025 holdout median APE {m['model']['mape']:.1%} (baseline {m['baseline']['mape']:.1%}), "
              f"rank corr {m['model']['spearman']:.2f} (baseline {m['baseline']['spearman']:.2f}), "
              f"busiest-fifth hit {m['model']['top_hit']:.0%}, quietest-fifth hit {m['model']['bottom_hit']:.0%}")  # fmt: skip
    forecasts = pd.concat(outs, ignore_index=True)
    store.write(forecasts, "forecasts")
    runs_df = pd.DataFrame(runs).astype({"run_id": "string", "target": "string", "features": "string", "metrics": "string"})
    store.write(runs_df, "model_runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
