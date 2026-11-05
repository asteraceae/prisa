"""End-to-end build on a tiny fake delivery shaped like the real Telefónica files (Nov 2026)."""

import duckdb
import pandas as pd
import pytest

from prisa import schemas
from prisa.enums import SourceFile
from prisa.ingest import telefonica, tourism
from prisa.ingest.__main__ import build

MOBILITY_HEADER = telefonica.RAW_COLUMNS[SourceFile.BARRI_BARRI]
BCN = ("08019", "Barcelona")


def write(path, header, rows, sep=","):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=header).to_csv(path, index=False, sep=sep)


@pytest.fixture
def delivery(tmp_path):
    raw = tmp_path / "raw"
    mov = raw / "movilidad/output_limite_legal/2024-05"
    write(mov / "movilidad_barrios_2024-05_origen.csv", MOBILITY_HEADER, [
        ("2024-05-11", "Sábado", "05", "Internacional", *BCN, "080190101", "el Raval", *BCN, "080190201", "el Fort Pienc", "120"),
        ("2024-05-11", "Sábado", "05", "Residente", *BCN, "080190103", "la Barceloneta", *BCN, "080190101", "el Raval", "0"),
    ])
    # M3 repeats keys (sub-rows of an unpublished dimension): they must be summed
    write(mov / "movilidad_mun_barrios_2024-05_origen.csv", MOBILITY_HEADER, [
        ("2024-05-11", "Sábado", "05", "Regional", *BCN, "080190101", "el Raval", "08101", "L'Hospitalet", "-", "-", "300"),
        ("2024-05-11", "Sábado", "05", "Regional", *BCN, "080190101", "el Raval", "08101", "L'Hospitalet", "-", "-", "45"),
        ("2024-05-11", "Sábado", "05", "Regional", "08101", "L'Hospitalet", "-", "-", *BCN, "080190903", "el Bon Pastor", "0"),
        ("2024-05-11", "Sábado", "05", "Regional", "08101", "L'Hospitalet", "-", "-", *BCN, "080190903", "el Bon Pastor", "0"),
    ])
    write(mov / "movilidad_municipios_2024-05_origen.csv", telefonica.RAW_COLUMNS[SourceFile.MUNI_MUNI], [
        ("2024-05-11", "Sábado", "05", "Regional", "08019", "Barcelona", "08101", "L'Hospitalet", "400"),
        ("2024-05-11", "Sábado", "05", "Regional", "08015", "Badalona", "08101", "L'Hospitalet", "90"),
    ])

    tur = raw / "turismo/output_limite_legal/202405"
    write(tur / "visitantes_unicos_municipio_yearmon.csv",
          ["año", "mes", "cod_municipio", "desc_municipio", "origen", "tipo_visitante", "visitantes"],
          [("2024", "202405", "08019", "Barcelona", "Internacional", "Turista", "900000")], sep=";")
    write(tur / "visitantes_unicos_municipio_date_dt.csv",
          ["año", "mes", "date_dt", "cod_municipio", "desc_municipio", "origen", "tipo_visitante", "visitantes"],
          [("2024", "202405", "2024-05-11", "08019", "Barcelona", "Nacional", "Excursionista", "40000")], sep=";")
    write(tur / "pernoctaciones_semana.csv",
          ["año", "mes", "semana", "periodo", "cod_municipio", "desc_municipio", "origen", "pernoctaciones", "visitantes"],
          [("2024", "202405", "19", "2024-05-06 | 2024-05-12", "08019", "Barcelona", "Internacional", "3", "51000")], sep=";")
    write(tur / "visitantes_unicos_barrio_date_dt.csv",
          ["año", "mes", "date_dt", "cod_municipio", "cod_barrio", "desc_municipio", "origen", "tipo_visitante", "visitantes"],
          [("2024", "202405", "2024-05-11", "08019", "080190206", "Barcelona", "Internacional", "Turista", "30000"),
           ("2024", "202405", "2024-05-11", "08019", "080190206", "Barcelona", "Regional", "Excursionista", "8000"),
           ("2024", "202405", "NA", "NA", "NA", "NA", "NA", "NA", "500")], sep=";")
    write(tur / "visitantes_regionales_barrio_yearmon.csv",
          ["año", "mes", "cod_barrio", "cod_municipio", "desc_municipio", "tipo_visitante",
           "cod_municipio_residencia", "municipio_residencia", "visitantes"],
          [("2024", "202405", "080190206", "08019", "Barcelona", "Excursionista", "08101", "L'Hospitalet", "2000"),
           ("2024", "202405", "080190206", "08019", "Barcelona", "Excursionista", "NA", "NA", "40")], sep=";")
    write(tur / "visitantes_internacionales_barrio_yearmon.csv",
          ["año", "mes", "cod_barrio", "cod_municipio", "desc_municipio", "tipo_visitante", "pais_residencia", "visitantes"],
          [("2024", "202405", "080190206", "08019", "Barcelona", "Turista", "Francia", "12000")], sep=";")
    write(tur / "visitantes_sociodemografico_barrio_yearmon.csv",
          ["año", "mes", "cod_barrio", "cod_municipio", "desc_municipio", "origen", "tipo_visitante",
           "genero", "edad", "nivel_renta", "visitantes"],
          [("2024", "202405", "080190206", "08019", "Barcelona", "Nacional", "Turista", "f", "3039", "Medio-Alto", "900"),
           ("2024", "202405", "080190206", "08019", "Barcelona", "Nacional", "Turista", "NA", "NA", "NA", "60")], sep=";")
    write(raw / "notes/readme_extra.csv", ["something"], [("else",)])
    return raw
    # fmt: skip


def test_identify_files():
    assert tourism.identify("visitantes_unicos_barrio_date_dt.csv") == "visitantes_unicos_barrio_date_dt"
    assert tourism.identify("VISITANTES_UNICOS_MUNICIPIO_YEARMON.parquet") == "visitantes_unicos_municipio_yearmon"
    assert telefonica.identify("movilidad_mun_barrios_2023-01_origen.csv") == SourceFile.BARRI_MUNI
    assert telefonica.identify("movilidad_municipios_2023-01_origen.csv") == SourceFile.MUNI_MUNI
    assert telefonica.identify("movilidad_barrios_2023-01_origen.csv") == SourceFile.BARRI_BARRI
    assert tourism.identify("movilidad_barrios_2023-01_origen.csv") is None


def test_build_whole_delivery(delivery, tmp_path):
    out = tmp_path / "processed"
    report = build(delivery, out)

    con = duckdb.connect()
    flows = con.execute(f"SELECT * FROM {telefonica.trip_flows_sql(out)} ORDER BY source, origin_zone_id").df()
    assert set(flows["source"]) == {"barri_barri", "barri_muni", "muni_muni"}
    m3 = flows[flows["source"] == "barri_muni"].set_index("origin_zone_id")
    assert m3.loc["080190101", "trips"] == 345  # sub-rows summed
    assert m3.loc["08101", "trips"] == 0 and m3.loc["08101", "censored"]
    assert len(flows[flows["source"] == "muni_muni"]) == 2  # both sources kept

    for name in ("presence_monthly", "presence_daily", "overnight_stays", "visitor_origin", "visitor_profile"):
        df = pd.read_parquet(out / f"{name}.parquet")
        assert schemas.validate(df.astype({c.name: c.dtype for c in schemas.TABLES[name].columns}),
                                schemas.TABLES[name]) == []  # fmt: skip

    daily = pd.read_parquet(out / "presence_daily.parquet")
    assert set(daily["zone_id"]) == {"08019", "080190206"}  # NA remainder row dropped
    origin = pd.read_parquet(out / "visitor_origin.parquet")
    assert origin.loc[origin["origin_name"] == "L'Hospitalet", "origin_code"].item() == "08101"
    assert (origin["origin_name"] == schemas.ANONYMISED).sum() == 1  # NA in a descriptive column stays
    profile = pd.read_parquet(out / "visitor_profile.parquet")
    assert set(profile["age_band"]) == {"30-39", schemas.ANONYMISED}

    res = pd.read_parquet(out / "tourism_residuals.parquet").set_index("file")
    assert res.loc["visitantes_unicos_barrio_date_dt", "visitors"] == 500
    assert "numbering: per_district" in report
    assert "notes/readme_extra.csv" in report


def test_unknown_visitor_type_fails(tmp_path):
    path = tmp_path / "visitantes_unicos_municipio_yearmon.csv"
    write(path, ["año", "mes", "cod_municipio", "desc_municipio", "origen", "tipo_visitante", "visitantes"],
          [("2024", "202405", "08019", "Barcelona", "Internacional", "Crucerista", "1")], sep=";")  # fmt: skip
    with pytest.raises(ValueError, match="tipo_visitante"):
        tourism.load(path)
