"""End-to-end build on a tiny fake delivery shaped like the documented Telefónica files."""

import pandas as pd
import pytest

from prisa import schemas
from prisa.enums import SourceFile
from prisa.ingest import telefonica, tourism
from prisa.ingest.__main__ import build, summary

MOBILITY_HEADER = telefonica.RAW_COLUMNS[SourceFile.BARRI_BARRI]


def write(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=header).to_csv(path, index=False)


@pytest.fixture
def delivery(tmp_path):
    raw = tmp_path / "raw"
    bcn = ("8019", "Barcelona")
    write(raw / "mov/barrios.csv", MOBILITY_HEADER, [
        ("2024-05-11", "Sábado", "5", "Internacional", *bcn, "80190101", "el Raval", *bcn, "80190205", "el Fort Pienc", "120"),
        ("2024-05-11", "Sábado", "5", "Residente", *bcn, "80190103", "la Barceloneta", *bcn, "80190101", "el Raval", "0"),
    ])
    write(raw / "mov/barrios_municipios.csv", MOBILITY_HEADER, [
        ("2024-05-11", "Sábado", "5", "Regional", *bcn, "80190101", "el Raval", "08101", "L'Hospitalet", "-", "-", "300"),
    ])
    write(raw / "mov/municipios.csv", telefonica.RAW_COLUMNS[SourceFile.MUNI_MUNI], [
        ("2024-05-11", "Sábado", "5", "Regional", "08019", "Barcelona", "08101", "L'Hospitalet", "310"),
        ("2024-05-11", "Sábado", "5", "Regional", "08015", "Badalona", "08101", "L'Hospitalet", "90"),
    ])

    muni = ["año", "mes", "cod_municipio", "desc_municipio", "origen", "tipo_visitante", "visitantes"]
    write(raw / "tur/visitantes_unicos_municipio_yearmon.csv", muni,
          [("2024", "202405", "08019", "Barcelona", "Internacional", "Turista", "900000")])
    write(raw / "tur/visitantes_unicos_municipio_date_dt.csv",
          ["año", "mes", "date_dt", "cod_municipio", "desc_municipio", "origen", "tipo_visitante", "visitantes"],
          [("2024", "202405", "2024-05-11", "08019", "Barcelona", "Nacional", "Excursionista", "40000")])
    write(raw / "tur/pernoctaciones_semana.csv",
          ["año", "mes", "semana", "periodo", "cod_municipio", "desc_municipio", "origen", "pernoctaciones", "visitantes"],
          [("2024", "202405", "19", "2024-05-06 / 2024-05-12", "08019", "Barcelona", "Internacional", "3", "51000")])
    write(raw / "tur/visitantes_unicos_barrio_date_dt.csv",
          ["año", "mes", "Date_dt", "cod_municipio", "cod_barrio", "desc_municipio", "origen", "tipo_visitante", "visitantes"],
          [("2024", "202405", "2024-05-11", "08019", "80190206", "Barcelona", "Internacional", "Turista", "30000"),
           ("2024", "202405", "2024-05-11", "08019", "80190206", "Barcelona", "Regional", "Excursionista", "8000")])
    write(raw / "tur/visitantes_regionales_barrio_yearmon.csv",
          ["año", "mes", "cod_barrio", "cod_municipio", "desc_municipio", "cod_municipio_residencia",
           "municipio_residencia", "tipo_visitante", "visitantes"],
          [("2024", "202405", "80190206", "08019", "Barcelona", "8101", "L'Hospitalet", "Excursionista", "2000"),
           ("2024", "202405", "80190206", "08019", "Barcelona", "NA", "NA", "Excursionista", "40")])
    write(raw / "tur/visitantes_internacionales_barrio_yearmon.csv",
          ["año", "mes", "cod_barrio", "cod_municipio", "desc_municipio", "pais_Residencia", "tipo_visitante", "visitantes"],
          [("2024", "202405", "80190206", "08019", "Barcelona", "Francia", "Turista", "12000")])
    write(raw / "tur/visitantes_sociodemografico_barrio_yearmon.csv",
          ["año", "mes", "cod_barrio", "cod_municipio", "desc_municipio", "origen", "tipo_visitante",
           "genero", "edad", "nivel_renta", "visitantes"],
          [("2024", "202405", "80190206", "08019", "Barcelona", "Internacional", "Turista", "f", "30-39", "Medio-Alto", "900"),
           ("2024", "202405", "80190206", "08019", "Barcelona", "Internacional", "Turista", "NA", "NA", "NA", "60")])
    write(raw / "notes/readme_extra.csv", ["something"], [("else",)])
    return raw
    # fmt: skip


def test_identify_tourism_files():
    assert tourism.identify("visitantes_unicos_barrio_date_dt_2024.csv") == "visitantes_unicos_barrio_date_dt"
    assert tourism.identify("VISITANTES_UNICOS_MUNICIPIO_YEARMON.parquet") == "visitantes_unicos_municipio_yearmon"
    assert tourism.identify("barrios.csv") is None


def test_build_whole_delivery(delivery, tmp_path, capsys):
    tables = build(delivery, tmp_path / "processed")

    assert set(tables) == {
        "trip_flows", "presence_monthly", "presence_daily", "overnight_stays", "visitor_origin", "visitor_profile",
    }  # fmt: skip
    for name, df in tables.items():
        assert schemas.validate(df, schemas.TABLES[name]) == []
        assert (tmp_path / "processed" / f"{name}.parquet").exists()

    flows = tables["trip_flows"]
    assert set(flows["source"]) == {"barri_barri", "barri_muni", "muni_muni"}
    # Barcelona -> L'Hospitalet at municipality level is dropped (barri level kept); Badalona row stays
    mm = flows[flows["source"] == "muni_muni"]
    assert mm["origin_zone_id"].tolist() == ["08015"]

    daily = tables["presence_daily"]
    assert set(daily["zone_id"]) == {"08019", "80190206"}
    assert set(daily["visitor_type"]) == {"tourist", "day_tripper"}

    origin = tables["visitor_origin"]
    assert origin.loc[origin["origin_name"] == "L'Hospitalet", "origin_code"].item() == "08101"
    assert origin.loc[origin["origin_name"] == schemas.ANONYMISED, "origin_code"].isna().all()
    assert set(origin["segment"]) == {"regional", "international"}

    profile = tables["visitor_profile"]
    assert (profile["gender"] == schemas.ANONYMISED).sum() == 1

    assert tables["overnight_stays"]["nights"].tolist() == [3]
    assert "skipped (not recognised): notes/readme_extra.csv" in capsys.readouterr().err

    report = summary(tables)
    assert "barri numbering: city_wide" in report  # el Fort Pienc = 80190205


def test_unknown_visitor_type_fails(tmp_path):
    path = tmp_path / "visitantes_unicos_municipio_yearmon.csv"
    write(path, ["año", "mes", "cod_municipio", "desc_municipio", "origen", "tipo_visitante", "visitantes"],
          [("2024", "202405", "08019", "Barcelona", "Internacional", "Crucerista", "1")])  # fmt: skip
    with pytest.raises(ValueError, match="tipo_visitante"):
        tourism.load(path)
