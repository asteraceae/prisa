import pandas as pd
import pytest

from prisa import collect, fares, geo, pressure, reference, schemas
from prisa.enums import Segment, SourceFile, TransportMode
from prisa.ingest import telefonica
from prisa.saturation import Assumptions, capacity_at, mode_share_from_table, saturation, stop_shares


def raw_rows(source, rows):
    cols = telefonica.RAW_COLUMNS[source]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows]).astype(str)


def test_codes_keep_leading_zero():
    assert geo.municipality_code(8019) == "08019"
    assert geo.municipality_code("8019.0") == "08019"
    assert geo.barri_code("-") is None
    assert geo.zone_id("08019", "080190103") == "080190103"
    assert geo.zone_id("08019", "80190103") == "080190103"  # spec-PDF form is padded
    assert geo.zone_id("08101", "-") == "08101"
    assert geo.district_of("080190903") == 9
    assert geo.barri_in_district("080190903") == 3
    with pytest.raises(ValueError):
        geo.barri_code("80800101")


def test_trip_flows_from_raw():
    raw = raw_rows(
        SourceFile.BARRI_MUNI,
        [
            ("2024-05-11", "Sábado", "5", "Internacional", "8019", "Barcelona", "080190101", "el Raval",
             "08101", "L'Hospitalet", "-", "-", "120"),
            ("2024-05-11", "Sábado", "5", "Residente", "8019", "Barcelona", "080190103", "la Barceloneta",
             "08101", "L'Hospitalet", "-", "-", "0"),
        ],
    )  # fmt: skip
    flows = telefonica.to_trip_flows(raw, SourceFile.BARRI_MUNI)
    assert flows["dest_zone_id"].tolist() == ["08101", "08101"]
    assert flows["segment"].tolist() == [Segment.INTERNATIONAL, Segment.RESIDENT]
    assert flows["censored"].tolist() == [False, True]
    assert schemas.validate(flows, schemas.TRIP_FLOWS) == []


def test_boundary_duplicates_dropped():
    day = pd.Timestamp("2024-05-11")
    flows = pd.DataFrame(
        {
            "day": [day, day, day],
            "origin_zone_id": pd.array(["080190101", "08019", "08101"], dtype="string"),
            "dest_zone_id": pd.array(["08101", "08101", "08015"], dtype="string"),
            "source": pd.array(["barri_muni", "muni_muni", "muni_muni"], dtype="string"),
        }
    )
    kept = telefonica.drop_barcelona_boundary_duplicates(flows)
    assert kept["origin_zone_id"].tolist() == ["080190101", "08101"]


def test_validate_reports_problems():
    df = schemas.TRIP_FLOWS.empty()
    assert schemas.validate(df, schemas.TRIP_FLOWS) == []
    bad = df.drop(columns="censored")
    assert any("missing" in p for p in schemas.validate(bad, schemas.TRIP_FLOWS))


def test_composite_skips_missing_components():
    day = pd.Timestamp("2024-05-11")
    comps = pd.DataFrame(
        {
            "day": [day, day, day],
            "zone_id": ["a", "a", "b"],
            "component": ["visitor_density", "transit_load", "visitor_density"],
            "raw": [1.0, 1.0, 1.0],
            "normalised": [0.8, 0.2, 0.5],
        }
    )
    out = pressure.composite(comps, {"visitor_density": 0.5, "transit_load": 0.5}, "test")
    assert out.set_index("zone_id")["value"].to_dict() == pytest.approx({"a": 0.5, "b": 0.5})


def test_saturation_ratio():
    day = pd.Timestamp("2024-05-11")
    flows = pd.DataFrame(
        {
            "day": [day],
            "origin_zone_id": ["z"],
            "dest_zone_id": ["y"],
            "segment": [Segment.INTERNATIONAL.value],
            "trips": [1000],
            "censored": [False],
        }
    )
    service = pd.DataFrame(
        {"day": [day], "line_id": ["bus:116"], "station_id": ["s1"], "direction": [0], "departures": [10],
         "departures_peak_hour": [2]}
    )  # fmt: skip
    stations = pd.DataFrame({"station_id": ["s1"], "zone_id": ["z"], "mode": ["bus"]})
    shares = stop_shares(service, stations, pd.Series({"bus:116": 100}))
    a = Assumptions(mode_share={Segment.INTERNATIONAL: {TransportMode.BUS: 0.5}}, peak_demand_share=0.3)
    out = saturation(flows, shares, a)
    # 1000 trips x 50% bus / (10 departures x 100 places)
    assert out["ratio_daily"].iloc[0] == pytest.approx(0.5)
    # 30% of demand in the peak hour, 20% of departures in it
    assert out["ratio_peak_hour"].iloc[0] == pytest.approx(0.75)


def test_fare_calendar_and_fairness():
    days = pd.DataFrame(
        {"day": pd.to_datetime(["2027-01-12", "2027-08-15"]), "expected_pressure": [0.1, 0.9]}
    )
    cal = fares.build_calendar(days, "v0", max_discount=0.3)
    assert cal["discount"].tolist() == pytest.approx([0.3, 0.0])

    products = reference.load(schemas.FARE_PRODUCTS)
    prices = fares.product_prices(cal, products)
    assert fares.never_above_published(prices).empty

    jan = prices[prices["day"] == "2027-01-12"].set_index("product_id")["price_today_eur"]
    assert jan["single"] == pytest.approx(2.03)  # 2.90 x 0.7
    assert jan["t_usual"] == pytest.approx(22.80)  # flat pass: no daily price
    assert fares.check_resident_fairness(prices, 22.80, 40, ["t_usual", "t_jove"]).empty


def test_reference_tables_validate():
    for table in (schemas.FARE_PRODUCTS, schemas.VEHICLE_TYPES, schemas.HOTSPOTS):
        assert len(reference.load(table)) > 0


def test_infer_barri_numbering():
    # la Verneda i la Pau is official barri 73 (district 10); the real data restarts per district
    assert geo.infer_barri_numbering(pd.Series(["080190103", "080191073"])) == "city_wide"
    assert geo.infer_barri_numbering(pd.Series(["080190103", "080190201"])) == "per_district"
    assert geo.infer_barri_numbering(pd.Series(["080190103"])) == "unknown"


def test_capacity_at_both_densities():
    vt = reference.load(schemas.VEHICLE_TYPES)
    four = capacity_at(vt, 4.0).set_index("vehicle_type")["capacity"]
    six = capacity_at(vt, 6.0).set_index("vehicle_type")["capacity"]
    # tram: 64 seated, 218 total at the assumed 4/m2 -> 38.5 m2 standing
    assert four["tram_citadis_302"] == pytest.approx(218)
    assert six["tram_citadis_302"] == pytest.approx(64 + 154 / 4 * 6)
    assert (six >= four).all()


def test_parse_fgc_snapshot(tmp_path):
    import gzip
    import json

    snap = tmp_path / "fgc" / "2026-10-24" / "150657Z.json.gz"
    snap.parent.mkdir(parents=True)
    rec = {"lin": "S1", "ut": "u1", "estacionat_a": None,
           "properes_parades": '{"parada": "PF"};{"parada": "SR"}',
           "ocupacio_mi_percent": "50", "ocupacio_ri_percent": "60",
           "ocupacio_m1_percent": "", "ocupacio_m2_percent": None}  # fmt: skip
    snap.write_bytes(gzip.compress(json.dumps({"results": [rec]}).encode()))
    (row,) = collect.parse_fgc(snap)
    assert row["occupancy_pct"] == 55.0
    assert row["station_id"] == "fgc:PF"
    assert row["observed_at"].hour == 15


def test_mode_share_table():
    t = reference.load(schemas.MODE_SHARE)
    assert t.groupby("segment")["share"].sum().round(2).eq(1.0).all()
    shares = mode_share_from_table(t)
    assert set(shares) == set(Segment)
    assert shares[Segment.INTERNATIONAL][TransportMode.METRO] == pytest.approx(0.30)
    assert "walk" not in {m.value for m in shares[Segment.RESIDENT]}
