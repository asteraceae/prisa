"""Transit supply on a tiny synthetic GTFS feed: 3 stops, 2 trips, no network."""

from datetime import date

import pandas as pd
import pytest

from prisa import schemas
from prisa.external import transit

STOPS = [
    ("A", "Stop A", "41.380", "2.150"),
    ("B", "Stop B", "41.390", "2.160"),
    ("C", "Stop C", "41.400", "2.170"),
]


def write(path, header, rows):
    path.write_text("\n".join([",".join(header), *(",".join(r) for r in rows)]) + "\n")


@pytest.fixture
def feed_dir(tmp_path):
    d = tmp_path / "gtfs"
    d.mkdir()
    write(d / "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"], STOPS)
    write(
        d / "routes.txt", ["route_id", "route_short_name", "route_long_name", "route_type"], [("R1", "T9", "Test", "0")]
    )
    # no direction_id: it has to be derived from geometry
    write(d / "trips.txt", ["route_id", "service_id", "trip_id"], [("R1", "WK", "t1"), ("R1", "ALL", "t2")])
    write(d / "stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], [
        ("t1", "07:10:00", "07:10:00", "A", "1"),
        ("t1", "07:20:00", "07:20:00", "B", "2"),
        ("t1", "07:30:00", "07:30:00", "C", "3"),
        ("t2", "08:50:00", "08:50:00", "C", "1"),
        ("t2", "09:00:00", "09:00:00", "B", "2"),
        ("t2", "25:05:00", "25:05:00", "A", "3"),  # after midnight, same service day
    ])  # fmt: skip
    write(d / "calendar.txt", ["service_id", *transit._WEEKDAYS, "start_date", "end_date"], [
        ("WK", "1", "1", "1", "1", "1", "0", "0", "20261101", "20261231"),
        ("ALL", "1", "1", "1", "1", "1", "1", "1", "20261101", "20261231"),
    ])  # fmt: skip
    # t2 does not run on Wednesday 4 Nov: weekday mean 4/5
    write(d / "calendar_dates.txt", ["service_id", "date", "exception_type"], [("ALL", "20261104", "2")])
    return d


@pytest.fixture
def gtfs(feed_dir):
    return transit.read_gtfs(feed_dir)


def test_read_trims_and_filters(gtfs):
    assert list(gtfs.stops["stop_id"]) == ["A", "B", "C"]
    assert len(gtfs.stop_times) == 6
    assert gtfs.trips["direction_id"].isna().all()
    assert gtfs.frequencies.empty


def test_bbox_drops_stops_outside(feed_dir):
    g = transit.read_gtfs(feed_dir, keep_stops=lambda s: s["stop_id"] != "C")
    assert set(g.stop_times["stop_id"]) == {"A", "B"}


def test_representative_week(gtfs):
    assert transit.representative_week(gtfs, date(2026, 11, 5)) == date(2026, 11, 2)
    # week of 7 Dec has the Immaculate Conception (Tue 8 Dec): skip to the next one
    assert transit.representative_week(gtfs, date(2026, 12, 8)) == date(2026, 12, 14)


def test_active_services_applies_exceptions(gtfs):
    act = transit.active_services(gtfs.calendar, gtfs.calendar_dates, pd.date_range("2026-11-02", "2026-11-08"))
    per_day = act.groupby(act["day"].dt.day)["service_id"].apply(set)
    assert per_day[2] == {"WK", "ALL"}
    assert per_day[4] == {"WK"}
    assert per_day[7] == {"ALL"}


def test_directions_derived(gtfs):
    calls = transit.stop_calls(gtfs)
    d = transit.assign_directions(gtfs.trips, calls, gtfs.stops)
    assert dict(zip(gtfs.trips["trip_id"], d)) == {"t1": 0, "t2": 1}


def test_summarise(gtfs):
    t = transit.summarise(gtfs, "tram", date(2026, 11, 2))
    s = t["service_by_daytype"].set_index(["station_id", "direction", "day_type"])
    # t1 runs Mon-Fri only, once
    assert s.loc[("tram:A", 0, "weekday"), "departures"] == 1
    assert s.loc[("tram:A", 0, "weekday"), "peak_hour"] == 7
    assert ("tram:A", 0, "saturday") not in s.index
    # t2 every day except Wednesday: 0.8 rounds to 1; 25:05 is hour 1
    assert s.loc[("tram:A", 1, "weekday"), "departures"] == 1
    assert s.loc[("tram:A", 1, "sunday_holiday"), "peak_hour"] == 1
    assert s.loc[("tram:C", 1, "saturday"), "peak_hour"] == 8
    assert len(s) == 3 * 2 + 3 * 2  # dir 0 on weekdays; dir 1 on all three day types

    ls = t["line_stops"]
    assert list(ls[ls["direction"] == 1]["station_id"]) == ["tram:C", "tram:B", "tram:A"]
    assert t["lines"].iloc[0].to_dict() == {
        "line_id": "tram:R1", "code": "T9", "mode": "tram", "operator": "tram", "vehicle_type": "tram_citadis_302",
    }  # fmt: skip

    tables = transit.finalise([t], geometry_df=None)  # validates every contract
    assert tables["stations"]["zone_id"].isna().all()
    with pytest.raises(ValueError):
        schemas.check(tables["stations"], schemas.STATIONS)  # zone_id is required once geometry exists


def test_peak_hour_counts_departures():
    calls = pd.DataFrame({
        "trip_id": ["a", "b", "c"],
        "stop_id": ["S", "S", "S"],
        "stop_sequence": [1, 1, 1],
        "secs": [7 * 3600 + 50 * 60, 8 * 3600 + 5 * 60, 8 * 3600 + 35 * 60],
    })  # fmt: skip
    trips = pd.DataFrame({"trip_id": ["a", "b", "c"], "route_id": "R", "service_id": "X", "direction": 0})
    services = pd.DataFrame({"service_id": "X", "day": pd.date_range("2026-11-02", "2026-11-08")})
    out = transit.departures_by_daytype(calls, trips, services).set_index("day_type")
    assert out.loc["weekday", "departures"] == 3
    assert out.loc["weekday", "peak_hour"] == 8
    assert out.loc["weekday", "departures_peak_hour"] == 2


def test_frequencies_expanded(feed_dir):
    write(feed_dir / "frequencies.txt", ["trip_id", "start_time", "end_time", "headway_secs"],
          [("t1", "10:00:00", "11:00:00", "600")])  # fmt: skip
    g = transit.read_gtfs(feed_dir)
    calls = transit.stop_calls(g)
    at_b = calls[(calls["trip_id"] == "t1") & (calls["stop_id"] == "B")]["secs"].sort_values()
    assert len(at_b) == 6
    assert at_b.iloc[0] == 10 * 3600 + 600  # 10 minutes after the first stop


def test_assign_zones():
    geom = pd.DataFrame({
        "zone_id": ["080190101", "080190102"],
        "wkt_wgs84": ["POLYGON ((2.0 41.0, 2.1 41.0, 2.1 41.1, 2.0 41.1, 2.0 41.0))",
                      "POLYGON ((2.1 41.0, 2.2 41.0, 2.2 41.1, 2.1 41.1, 2.1 41.0))"],
    })  # fmt: skip
    st = pd.DataFrame({"station_id": ["x", "y", "z"], "lat": [41.05, 41.05, 41.5], "lon": [2.05, 2.15, 2.15]})
    out = transit.assign_zones(st, geom)
    assert list(out["zone_id"].astype(object).where(out["zone_id"].notna(), None)) == ["080190101", "080190102", None]


def test_expand_service_daily(gtfs):
    sbd = transit.finalise([transit.summarise(gtfs, "tram", date(2026, 11, 2))], None)["service_by_daytype"]
    cal = pd.DataFrame({
        "day": pd.to_datetime(["2023-12-23", "2023-12-24", "2023-12-25", "2023-12-27"]),
        "dow": [5, 6, 0, 2],
        "public_holiday": [False, False, True, False],
    })  # fmt: skip
    daily = transit.expand_service_daily(sbd, cal, "2023-12-23", "2023-12-26")
    per_day = daily.groupby("day")["departures"].sum()
    assert list(per_day.index.day) == [23, 24, 25]  # 27th is outside the range
    # Saturday and Sunday: only t2 (3 stops); Christmas Monday is a holiday, so Sunday's profile
    assert per_day.tolist() == [3, 3, 3]
    weekday = transit.expand_service_daily(sbd, cal.assign(public_holiday=False), "2023-12-25", "2023-12-25")
    assert weekday["departures"].sum() == 6
