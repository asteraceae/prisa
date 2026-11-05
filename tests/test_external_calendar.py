from datetime import date

import pandas as pd
import pytest

from prisa import schemas
from prisa.external import calendar as cal

# Generalitat rows as published, including the junk the real dataset carries
LOCAL_ROWS = [
    {"data": f"{d}T00:00:00.000", "ajuntament_o_nucli_municipal": n, "codi_municipi_ine": "08019", "pedania": "000"}
    for d, n in [
        ("2023-06-05", "Barcelona"), ("2023-09-25", "Barcelona"), ("2023-06-23", "C. A. de Catalunya"),
        ("2024-01-16", "Barcelona"), ("2024-05-20", "Barcelona"), ("2024-09-24", "Barcelona"),
        ("2025-06-09", "Barcelona"), ("2025-09-24", "Barcelona"), ("2025-06-12", "null"),
        ("2026-05-25", "Barcelona"), ("2026-09-24", "Barcelona"),
    ]
]  # fmt: skip


@pytest.fixture(scope="module")
def days():
    return cal.build_calendar_days(LOCAL_ROWS).set_index("day")


def test_holiday_scopes(days):
    assert days.loc["2025-09-24", ["holiday_name", "holiday_scope"]].tolist() == ["La Mercè", "local"]
    assert days.loc["2024-04-01", "holiday_scope"] == "catalan"  # Easter Monday
    assert days.loc["2025-12-25", "holiday_scope"] == "national"
    assert days.loc["2024-09-11", "holiday_scope"] == "catalan"
    assert not days.loc["2024-01-16", "public_holiday"]  # stray row dropped
    assert not days.loc["2025-06-12", "public_holiday"]  # test row dropped
    assert days.loc["2023-06-05", "public_holiday"] and not days.loc["2023-05-29", "public_holiday"]


def test_2027_local_holidays_assumed(days):
    assert days.loc["2027-05-17", "holiday_name"] == "Dilluns de Pasqua Granada (assumed)"
    assert days.loc["2027-09-24", "holiday_scope"] == "local"
    assert days.loc["2027-03-29", "holiday_scope"] == "catalan"  # Easter Monday, Ordre EMT/52/2026
    assert days["holiday_name"].str.contains("assumed").sum() == 2


def test_bridge_days(days):
    bridges = set(days.index[days["is_bridge_day"]].strftime("%Y-%m-%d"))
    assert "2025-05-02" in bridges  # Fri after Thu 1 May
    assert "2024-09-23" in bridges  # Mon before Tue La Mercè
    assert "2027-12-07" in bridges  # Tue between Mon 6 and Wed 8 Dec
    assert "2023-12-07" in bridges  # Thu between Wed 6 and Fri 8 Dec
    assert "2025-12-24" not in bridges  # Wed: Tue 23 is a working day
    assert not days.loc[days["is_bridge_day"], "public_holiday"].any()
    assert not days.loc[days["is_bridge_day"], "is_weekend"].any()


def test_bridge_days_synthetic():
    day = pd.Series(pd.date_range("2024-01-01", "2024-01-14"))  # Mon .. Sun
    holiday = day.isin(pd.to_datetime(["2024-01-02", "2024-01-11"]))  # Tue, Thu
    got = day[cal.bridge_days(day, holiday).astype(bool)].dt.strftime("%m-%d").tolist()
    assert got == ["01-12"]  # Fri after Thu; Mon 1 Jan has no known Sunday before it


def test_school_holidays(days):
    assert days.loc["2027-03-22", "school_holiday"]  # Easter 2027
    assert days.loc["2027-08-01", "school_holiday"]
    assert not days.loc["2027-06-21", "school_holiday"]  # last day of class
    assert not days.loc["2026-09-08", "school_holiday"]  # first day of class
    assert days.loc["2026-12-31", "school_holiday"]


def test_calendar_schema(days):
    df = days.reset_index()
    assert len(df) == (date(2027, 12, 31) - date(2023, 1, 1)).days + 1
    assert schemas.validate(df, schemas.CALENDAR_DAYS) == []


WEATHER = {
    "daily": {
        "time": ["2022-12-31", "2023-01-01", "2023-01-02", "2023-01-03"],
        "precipitation_sum": [0.0, 1.5, 0.0, None],
        "temperature_2m_max": [14.0, 15.8, 15.2, None],
        "temperature_2m_min": [6.0, 6.6, 6.0, None],
    }
}


def test_weather_daily():
    w = cal.build_weather_daily(WEATHER)
    assert w["day"].dt.strftime("%Y-%m-%d").tolist() == ["2023-01-01", "2023-01-02"]  # trailing empty day trimmed
    assert schemas.validate(w, schemas.WEATHER_DAILY) == []
    clim = cal.climatology(WEATHER, years=(2022, 2023), window=1)
    proxy = cal.weather_proxy(clim, date(2027, 1, 1), date(2027, 1, 2))
    assert proxy["tmax_c"].iloc[0] == 15.8


def test_events_from_reference_csv():
    curated = cal.read_curated()
    seasons = {
        "2025-26": {
            "matches": [
                {"date": "2025-09-14", "team1": "FC Barcelona", "team2": "Valencia CF"},
                {"date": "2025-10-18", "team1": "FC Barcelona", "team2": "Girona FC"},
                {"date": "2025-11-22", "team1": "FC Barcelona", "team2": "Athletic Club"},
                {"date": "2025-11-08", "team1": "RC Celta", "team2": "FC Barcelona"},
            ]
        }
    }
    football = cal.football_matches(seasons)
    crosswalk = pd.DataFrame({"zone_id": ["080190301", "080190402", "080191007"], "barri_number": ["11", "20", "70"]})
    ev = cal.build_events(curated, football, {2023: 55958}, crosswalk).set_index("event_id")
    assert schemas.validate(ev.reset_index(), schemas.EVENTS) == []
    assert ev.loc["mwc_2027", "zone_id"] == "08101"  # Fira Gran Via, L'Hospitalet
    assert ev.loc["primavera_2027", "zone_id"] == "080191007"
    assert pd.isna(ev.loc["merce_2025", "zone_id"])  # city-wide
    assert pd.isna(ev.loc["santjordi_2025", "zone_id"])
    assert ev.loc["fcb_20250914", "zone_id"] == "08217"  # Estadi Johan Cruyff
    assert ev.loc["fcb_20251018", "zone_id"] == "080190301"  # Montjuïc
    assert ev.loc["fcb_20251122", "zone_id"] == "080190402"  # back at Camp Nou
    assert "fcb_20251108" not in ev.index  # away match
    assert ev.loc["eo_coldplay_20230524", "expected_attendance"] == 55958
    # without a crosswalk barri venues stay null, other municipalities still resolve
    no_xw = cal.build_events(curated).set_index("event_id")
    assert pd.isna(no_xw.loc["primavera_2027", "zone_id"]) and no_xw.loc["f1_2026", "zone_id"] == "08135"
