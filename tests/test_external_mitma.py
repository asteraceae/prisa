"""MITMA hourly profiles on a few fake rows shaped like the published daily files (offline)."""

import gzip
from datetime import date

import pandas as pd
import pytest

from prisa import schemas
from prisa.external import mitma

HEADER = (
    "fecha|periodo|origen|destino|distancia|actividad_origen|actividad_destino|"
    "estudio_origen_posible|estudio_destino_posible|residencia|renta|edad|sexo|viajes|viajes_km"
)


def row(day, hour, o, d, viajes, res="08", act_o="casa", act_d="trabajo_estudio"):
    return f"{day}|{hour:02d}|{o}|{d}|2-10|{act_o}|{act_d}|no|no|{res}|10-15|NA|NA|{viajes}|1.0"


ROWS = [
    # Monday 2024-11-11, a plain weekday
    row("20241111", 8, "0801901", "0801902", "10.0"),  # Ciutat Vella -> Eixample
    row("20241111", 8, "0801902", "0801902", "6.0"),  # inside Eixample
    row("20241111", 18, "0801902", "0801901", "4.0", res="28", act_o="no_frecuente", act_d="casa"),
    row("20241111", 9, "08101", "0801910", "5.0"),  # L'Hospitalet -> Sant Martí
    row("20241111", 9, "08101", "08015", "99.0"),  # never touches Barcelona: dropped
    row("20241111", 9, "0801901_AD", "08015", "99.0"),  # look-alike code: dropped
    # Sunday 2024-11-17
    row("20241117", 12, "0801901", "0801906", "3.0", act_d="no_frecuente"),
    row("20241117", 20, "0801906", "0801901", "1.0", act_o="frecuente", act_d="casa"),
]


@pytest.fixture
def raw(tmp_path):
    src = tmp_path / "20241111_Viajes_distritos.csv.gz"
    with gzip.open(src, "wt") as f:
        f.write("\n".join([HEADER, *ROWS]) + "\n")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    kept = mitma.filter_file(src, raw_dir / "viajes_distritos_20241111.parquet")
    assert kept == 6
    return mitma.load_raw(raw_dir)


def test_filter_keeps_only_barcelona_districts(raw):
    ends = set(raw["origen"]) | set(raw["destino"])
    assert "08015" not in ends and "0801901_AD" not in ends
    assert raw["viajes"].sum() == pytest.approx(29.0)
    assert mitma.district_code(pd.Series(["0801901", "0801910", "08019", "08101", "0801901_AD"])).tolist() == [
        1, 10, pd.NA, pd.NA, pd.NA,
    ]  # fmt: skip


def test_day_type_holidays():
    days = pd.Series(pd.to_datetime([
        "2024-11-11",  # Monday
        "2024-11-16",  # Saturday
        "2024-11-17",  # Sunday
        "2024-09-24",  # La Mercè (Tuesday)
        "2023-09-25",  # La Mercè moved off Sunday 24 Sep
        "2024-05-20",  # Pentecost Monday
        "2024-06-24",  # Sant Joan (Catalan holiday)
        "2024-12-26",  # Sant Esteve (Catalan holiday)
        "2024-12-24",  # Christmas Eve: a working day
    ]))  # fmt: skip
    assert mitma.day_type(days).tolist() == [
        "weekday", "saturday", "sunday_holiday", "sunday_holiday", "sunday_holiday",
        "sunday_holiday", "sunday_holiday", "sunday_holiday", "weekday",
    ]  # fmt: skip


def test_traveller():
    df = pd.DataFrame({
        "residencia": ["08", "28", "08", "08"],
        "actividad_origen": ["casa", "casa", "no_frecuente", "frecuente"],
        "actividad_destino": ["trabajo_estudio", "casa", "casa", "no_frecuente"],
    })  # fmt: skip
    assert mitma.traveller(df).tolist() == ["local", "visitor", "visitor", "visitor"]


def test_hourly_profile_shares_sum_to_one(raw):
    prof = mitma.hourly_profile(raw)
    group = ["scope", "district_code", "day_type", "traveller"]
    sums = prof.groupby(group, dropna=False)["share"].sum()
    assert sums.to_numpy() == pytest.approx(1.0)
    assert (prof.groupby(group, dropna=False).size() == 24).all()

    city = prof[(prof["scope"] == "city") & (prof["day_type"] == "weekday") & (prof["traveller"] == "all")]
    assert city["trips"].sum() == pytest.approx(25.0)  # each trip once
    assert city.set_index("hour").loc[8, "share"] == pytest.approx(16 / 25)

    # Eixample (2): Ciutat Vella->Eixample, inside Eixample (once), Eixample->Ciutat Vella
    eix = prof[(prof["district_code"] == 2) & (prof["day_type"] == "weekday") & (prof["traveller"] == "all")]
    assert eix["trips"].sum() == pytest.approx(20.0)
    assert eix.set_index("hour").loc[18, "share"] == pytest.approx(4 / 20)

    vis = prof[(prof["scope"] == "city") & (prof["day_type"] == "weekday") & (prof["traveller"] == "visitor")]
    assert vis.set_index("hour").loc[18, "share"] == pytest.approx(1.0)

    peaks = mitma.peak_share(prof)
    top = peaks[(peaks["scope"] == "city") & (peaks["day_type"] == "weekday") & (peaks["traveller"] == "all")].iloc[0]
    assert (top["peak_hour"], top["peak_share"]) == (8, pytest.approx(16 / 25))
    sun = peaks[(peaks["scope"] == "city") & (peaks["day_type"] == "sunday_holiday") & (peaks["traveller"] == "all")]
    assert sun["peak_hour"].iloc[0] == 12 and sun["n_days"].iloc[0] == 1


def test_external_flows_contract(raw):
    flows = mitma.external_flows(raw)
    assert schemas.validate(flows, schemas.EXTERNAL_FLOWS) == []
    assert set(flows["source"]) == {"mitma_distritos"}
    assert flows["trips"].sum() == pytest.approx(29.0)
    assert flows["day"].min() == pd.Timestamp("2024-11-11")


def test_sample_design():
    days = mitma.sample_days()
    assert len(days) == 63 and all(d < date(2025, 7, 1) for d in days)
    types = mitma.day_type(pd.Series(pd.to_datetime(days))).value_counts()
    assert types["saturday"] == 9 and types["sunday_holiday"] >= 9 + 6
