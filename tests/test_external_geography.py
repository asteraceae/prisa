"""Offline checks for prisa.external.geography on tiny inline fixtures."""

import io
import zipfile

import pandas as pd
import pytest

from prisa import schemas
from prisa.external import geography as g

# official numbering is city-wide; Telefónica restarts it in every district
BARRIS = pd.DataFrame(
    [
        (1, "Ciutat Vella", 1, "el Raval"),
        (1, "Ciutat Vella", 4, "Sant Pere, Santa Caterina i la Ribera"),
        (5, "Sarrià-Sant Gervasi", 26, "Sant Gervasi - Galvany"),
        (9, "Sant Andreu", 57, "la Trinitat Vella"),
        (9, "Sant Andreu", 59, "el Bon Pastor"),
        (9, "Sant Andreu", 58, "Baró de Viver"),
    ],
    columns=["district_code", "district_name", "barri_number", "barri_name"],
)


def test_crosswalk_ranks_within_district():
    cw = g.build_crosswalk(BARRIS).set_index("barri_number")["zone_id"]
    assert cw[1] == "080190101"
    assert cw[4] == "080190102"
    assert cw[26] == "080190501"
    assert (cw[57], cw[58], cw[59]) == ("080190901", "080190902", "080190903")


def test_crosswalk_verification():
    cw = g.build_crosswalk(BARRIS)
    telefonica = {
        "080190101": "El Raval",
        "080190102": "Sant Pere Santa Caterina i la Ribera",
        "080190501": "Sant Gervasi-Galvany",
        "080190901": "la Trinitat Vella",
        "080190902": "Baró de Viver",
        "080190903": "el Bon Pastor",
    }
    g.verify_crosswalk(cw, telefonica)

    swapped = telefonica | {"080190902": "el Bon Pastor", "080190903": "Baró de Viver"}
    with pytest.raises(ValueError, match="080190902"):
        g.verify_crosswalk(cw, swapped)
    with pytest.raises(ValueError, match="not in crosswalk"):
        g.verify_crosswalk(cw, telefonica | {"080190904": "Sant Andreu"})


@pytest.mark.parametrize(
    "a, b",
    [
        ("Sant Pere, Santa Caterina i la Ribera", "SANT PERE SANTA CATERINA I LA RIBERA"),
        ("Sants - Badal", "Sants-Badal"),
        ("l'Antiga Esquerra de l'Eixample", "Antiga Esquerra de l Eixample"),
        ("el Camp d'en Grassot i Gràcia Nova", "Camp d'en Grassot i Gracia Nova"),
    ],
)
def test_normalise_name(a, b):
    assert g.normalise_name(a) == g.normalise_name(b)


def test_normalise_name_keeps_distinct_barris_apart():
    assert g.normalise_name("Sants") != g.normalise_name("Sants - Badal")
    assert g.normalise_name("Sant Gervasi - Galvany") != g.normalise_name("Sant Gervasi - la Bonanova")


@pytest.mark.parametrize(
    "ine, amb",
    [
        ("Hospitalet de Llobregat, L'", "l-hospitalet-de-llobregat"),
        ("Prat de Llobregat, El", "el-prat-de-llobregat"),
        ("Sant Vicenç dels Horts", "sant-vicenc-dels-horts"),
        ("Palma de Cervelló, La", "la-palma-de-cervello"),
    ],
)
def test_slug_matches_amb(ine, amb):
    assert g.slug(ine) == amb
    assert amb in g.AMB_SLUGS


def test_area_km2():
    square = "POLYGON ((430000 4580000, 431000 4580000, 431000 4582000, 430000 4582000, 430000 4580000))"
    assert g.area_km2(square) == pytest.approx(2.0)
    multi = (
        "MULTIPOLYGON (((0 0, 1000 0, 1000 1000, 0 1000, 0 0)), "
        "((2000 0, 2500 0, 2500 1000, 2000 1000, 2000 0)))"
    )
    assert g.area_km2(multi) == pytest.approx(1.5)


def _square(lon, lat, d=0.01):
    return f"POLYGON (({lon} {lat}, {lon + d} {lat}, {lon + d} {lat + d}, {lon} {lat + d}, {lon} {lat}))"


@pytest.fixture
def two_barris():
    barris = BARRIS.iloc[:2].assign(
        wkt_etrs89=None, wkt_wgs84=[_square(2.16, 41.37), _square(2.17, 41.37)]
    )
    cw = g.build_crosswalk(barris)
    return barris, cw, g.barri_geometry(barris, cw)


def test_barri_geometry_and_locate(two_barris):
    _, _, geom = two_barris
    assert list(geom["zone_id"]) == ["080190101", "080190102"]
    assert geom.loc[0, "centroid_lon"] == pytest.approx(2.165)
    assert geom.loc[0, "centroid_lat"] == pytest.approx(41.375)
    zones = g.locate(pd.Series([2.165, 2.175, 2.5]), pd.Series([41.375, 41.375, 41.375]), geom)
    assert zones.tolist()[:2] == ["080190101", "080190102"] and pd.isna(zones[2])


def _hut_zip(tmp_path, text, name="2023_4T_hut_comunicacio_opendata.csv.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name.removesuffix(".zip"), text)
    return path


HUT_HEADER = (
    "N_EXPEDIENT,CODI_DISTRICTE,NOM_DISTRICTE,CODI_BARRI,NOM_BARRI,TIPUS_CARRER,CARRER,TIPUS_NUM,NUM1,LLETRA1,"
    "NUM2,LLETRA2,BLOC,PORTAL,ESCALA,PIS,PORTA,NUMERO_REGISTRE_GENERALITAT,NUMERO_PLACES,LONGITUD_X,LATITUD_Y"
)


def test_read_hut_handles_unquoted_commas_and_glued_header(tmp_path):
    rows = [
        "01-1,01,CIUTAT VELLA,04,Sant Pere, Santa Caterina i la Ribera,Carrer,X,1,5,,,,,,,1,,HUTB-1,4,2.175,41.375",
        "01-2,01,CIUTAT VELLA,01,el Raval,Carrer,Y,1,9,,,,,,,1,2,HUTB-2,6,2.165,41.375",
    ]
    hut = g.read_hut(_hut_zip(tmp_path, HUT_HEADER + rows[0] + "\r" + rows[1] + "\r"))
    assert hut["barri_number"].tolist() == [4, 1]
    assert hut["places"].tolist() == [4, 6]
    assert hut["registry"].tolist() == ["HUTB-1", "HUTB-2"]


def test_read_hut_without_places_is_skipped(tmp_path):
    assert g.read_hut(_hut_zip(tmp_path, "N_EXPEDIENT,CODI_DISTRICTE,NOM_DISTRICTE,CODI_BARRI,NOM_BARRI\n")) is None


def test_accommodation_dedupes_and_places_by_coordinates(tmp_path, two_barris):
    _, cw, geom = two_barris
    rows = [
        "a,01,CV,01,el Raval,C,X,1,1,,,,,,,1,1,HUTB-1,5,2.165,41.375",
        "b,01,CV,,,C,X,1,1,,,,,,,1,1,HUTB-1,,2.165,41.375",  # repeat without barri: dropped
        "c,01,CV,,,C,X,1,1,,,,,,,1,1,HUTB-2,3,2.175,41.375",  # no barri code: placed by point
    ]
    (tmp_path / g.HUT).mkdir()
    _hut_zip(tmp_path / g.HUT, HUT_HEADER + "\n" + "\n".join(rows) + "\n")
    _hut_zip(tmp_path / g.HUT, HUT_HEADER + "\n" + rows[0] + "\n", "2023_1T_hut_comunicacio.csv.zip")
    acc = g.accommodation_table(cw, geom, raw_dir=tmp_path).set_index("zone_id")
    assert acc.loc["080190101", "tourist_flat_beds"] == 5  # mean of 5 and 5
    assert acc.loc["080190102", "tourist_flat_beds"] == 2  # mean of 0 and 3, rounded half to even
    assert acc["hotel_beds"].isna().all()


def test_zones_table_marks_amb(two_barris):
    barris, cw, _ = two_barris
    barris = barris.assign(wkt_etrs89="POLYGON ((0 0, 1000 0, 1000 1000, 0 1000, 0 0))")
    munis = {"08101": "Hospitalet de Llobregat, L'", "08001": "Abrera"} | {
        f"09{i:03d}": s for i, s in enumerate(sorted(g.AMB_SLUGS - {"barcelona", "l-hospitalet-de-llobregat"}))
    }
    zones = g.zones_table(barris, cw, munis).set_index("zone_id")
    assert zones.loc["080190101", "barri_number"] == 1
    assert zones.loc["080190101", "area_km2"] == pytest.approx(1.0)
    assert zones.loc["08019", "area_km2"] == pytest.approx(2.0)
    assert bool(zones.loc["08101", "in_amb"]) and not bool(zones.loc["08001", "in_amb"])
    assert zones["in_amb"].sum() == 2 + 36


def test_hotspots_resolve():
    every_barri = pd.DataFrame({
        "district_code": [(n - 1) // 10 + 1 for n in range(1, 74)],
        "district_name": "d",
        "barri_number": range(1, 74),
        "barri_name": "b",
    })  # fmt: skip
    cw = g.build_crosswalk(every_barri)
    hot = g.hotspots_with_zones(cw).set_index("hotspot_id")
    assert schemas.validate(hot.reset_index(), schemas.HOTSPOTS) == []
    assert hot.loc["sagrada_familia", "zone_id"] == "080190106"  # barri 6, 6th in fake district 1
    assert pd.isna(hot.loc["bus_turistic", "zone_id"])
    with pytest.raises(ValueError, match="unknown barri_number"):
        g.hotspots_with_zones(cw.iloc[:5])


def test_zone_geometry_contract_is_named():
    assert g.BARRI_GEOMETRY.name == "barri_geometry"
    assert g.BARRI_GEOMETRY.column_names == ["zone_id", "wkt_wgs84", "centroid_lat", "centroid_lon"]
