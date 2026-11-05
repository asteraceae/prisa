"""Ridership and occupancy loaders on tiny fixtures shaped like the real files (Nov 2026). Offline."""

import gzip
import json
from datetime import datetime

import openpyxl
import pandas as pd
import pytest

from prisa import schemas
from prisa.external import ridership as rs

MONTHS = ["GENER", "FEBRER", "MARÇ"]


def metro_block(ws, top, title, line, stations):
    """One line block as TMB lays it out: title, header (B=line, D..=months), stations in C, TOTAL."""
    ws.cell(top, 2, title)
    ws.cell(top + 3, 2, line)
    for j, m in enumerate(MONTHS):
        ws.cell(top + 3, 4 + j, m)
    ws.cell(top + 3, 4 + len(MONTHS), "ACUMULAT")
    r = top + 5
    for name, values in stations:
        ws.cell(r, 3, name)
        for j, v in enumerate(values):
            ws.cell(r, 4 + j, v)
        ws.cell(r, 4 + len(MONTHS), sum(values))
        r += 1
    ws.cell(r, 2, "TOTAL")
    for j in range(len(MONTHS)):
        ws.cell(r, 4 + j, sum(v[j] for _, v in stations))
    return r + 3


@pytest.fixture
def metro_xlsx(tmp_path):
    wb = openpyxl.Workbook()
    mens = wb.active
    mens.title = "Mensuals"
    r = metro_block(mens, 2, "VIATGERS REALS LÍNIA 1 (MENSUAL)", "LÍNIA  1",
                    [("R.J.OLIVERAS", [100, 110, 0]), ("PL. DE SANTS", [50.5, 60.25, 70])])
    r = metro_block(mens, r, "VIATGERS REALS LÍNIA 9/10 NORD (MENSUAL)", "LÍNIA  9/10 NORD",
                    [("TORRE BARÓ|VALLBONA", [7, 8, 9])])
    # summary table under the last block, as in the 2024 file: must be ignored
    mens.cell(r + 2, 3, "L-1")
    mens.cell(r + 2, 4, 999999)
    fei = wb.create_sheet("Feiners")
    metro_block(fei, 2, "EVOLUCIÓ DE VIATGERS/DIA A LA LÍNIA 1 EN FEINERS", "LÍNIA  1",
                [("RBLA. JUST OLIVERAS", [5.0, 5.5, 6.0]), ("PLAÇA DE SANTS", [2.5, 3.0, 3.5])])
    path = tmp_path / "tmb_metro_2024.xlsx"
    wb.save(path)
    return path


@pytest.fixture
def bus_xlsx(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2024"
    ws.cell(3, 3, "Resum de les dades mensuals i diàries de viatgers (1er semestre) a l'any 2024")
    ws.cell(6, 2, "LÍNIES")
    for j, m in enumerate(MONTHS):
        ws.cell(5, 3 + 2 * j, m)
        ws.cell(6, 3 + 2 * j, "TOTAL MENSUAL")
        ws.cell(6, 4 + 2 * j, "V/D FEINERS")
    for i, (line, vals) in enumerate([("116", [78382, 3025.6, 85062, 3469.5, 68215, 2866.2]),
                                      ("H12", [500000, 20000, 510000, 21000, None, None]),
                                      ("TOTAL", [1, 1, 1, 1, 1, 1])]):
        ws.cell(7 + i, 2, line)
        for j, v in enumerate(vals):
            ws.cell(7 + i, 3 + j, v)
    path = tmp_path / "tmb_bus_2024.xlsx"
    wb.save(path)
    return path


def test_tmb_metro(metro_xlsx):
    df = schemas.check(rs.parse_tmb_metro(metro_xlsx, 2024), schemas.RIDERSHIP)
    tot = df[df.statistic == "total"]
    # zero months dropped, TOTAL rows and the summary table skipped
    assert len(tot) == 2 + 3 + 3
    assert tot.passengers.sum() == pytest.approx(100 + 110 + 50.5 + 60.25 + 70 + 24)
    # abbreviated (Mensuals) and long (Feiners) names give the same station id
    assert set(df.station_id) == {"tmb_name:RBLA. JUST OLIVERAS", "tmb_name:PLAÇA DE SANTS",
                                  "tmb_name:TORRE BARÓ - VALLBONA"}
    assert set(df.line_id) == {"tmb_name:L1", "tmb_name:L9/10N"}
    aw = df[(df.statistic == "avg_weekday") & (df.station_id == "tmb_name:PLAÇA DE SANTS")]
    assert aw.set_index("period_start").passengers[pd.Timestamp("2024-03-01")] == 3.5


def test_tmb_bus(bus_xlsx):
    df = schemas.check(rs.parse_tmb_bus(bus_xlsx, 2024), schemas.RIDERSHIP)
    assert df.station_id.isna().all()
    assert set(df.line_id) == {"tmb_name:116", "tmb_name:H12"}
    l116 = df[df.line_id == "tmb_name:116"].pivot(index="period_start", columns="statistic", values="passengers")
    assert l116.loc["2024-02-01", "total"] == 85062
    assert l116.loc["2024-03-01", "avg_weekday"] == pytest.approx(2866.2)
    assert len(df[df.line_id == "tmb_name:H12"]) == 4  # empty March cells dropped


def test_tmb_names():
    assert rs.tmb_station_name("Z.UNIVERSITÀRIA") == "ZONA UNIVERSITÀRIA"
    assert rs.tmb_station_name("ARTIGUES | S. ADRIÀ") == "ARTIGUES - SANT ADRIÀ"
    assert rs.tmb_station_name("PARAL.LEL") == rs.tmb_station_name("PARAL·LEL")
    assert rs.tmb_station_name("VALL D´HEBRON") == "VALL D'HEBRON"
    assert rs.tmb_metro_line("LÍNIA  9/10 SUD") == "L9/10S"
    assert rs.match_key("Pl. Catalunya") == rs.match_key("Plaça de Catalunya")


@pytest.fixture
def renfe_csv(tmp_path):
    path = tmp_path / "renfe.csv"
    path.write_text(
        "CODIGO_ESTACION;NOMBRE_ESTACION;NUCLEO_CERCANIAS;TRAMO_HORARIO;VIAJEROS_SUBIDOS;VIAJEROS_BAJADOS\n"
        "71801;BARCELONA-SANTS;BARCELONA;07:00 - 07:30;30;10\n"
        "71801;BARCELONA-SANTS;BARCELONA;07:30 - 08:00;10;10\n"
        "71801;BARCELONA-SANTS;BARCELONA;07:30 - 08:00;20;0\n"  # second row for the same slot: summed
        "79500;MATARO;BARCELONA;18:00 - 18:30;40;80\n",
        encoding="utf-8",
    )
    return path


def test_renfe_and_hourly_profile(renfe_csv):
    df = schemas.check(rs.parse_renfe(renfe_csv), schemas.RIDERSHIP)
    b = df[df.statistic == "boardings"].set_index(["station_id", "period_start"]).passengers
    assert b[("renfe_code:71801", pd.Timestamp("2018-01-01 07:30"))] == 30
    prof = schemas.check(rs.hourly_profile(rs.read_renfe(renfe_csv)), rs.HOURLY_PROFILE)
    assert len(prof) == 48
    for _, g in prof.groupby(["source", "scope", "day_type"]):
        assert g.share.sum() == pytest.approx(1.0)
    allst = prof[prof.scope == "rodalies_bcn_stations"].set_index("hour").share
    assert allst[7] == pytest.approx(60 / 100) and allst[18] == pytest.approx(40 / 100)
    city = prof[prof.scope == "rodalies_bcn_city_stations"].set_index("hour").share
    assert city[7] == pytest.approx(1.0)


def snapshot(root, when: datetime, results):
    path = root / "fgc" / when.strftime("%Y-%m-%d") / f"{when.strftime('%H%M%S')}Z.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps({"total_count": len(results), "results": results}).encode()))


def train(lin, ut, pct, stop="PC"):
    return {"lin": lin, "ut": ut, "estacionat_a": stop, "properes_parades": "",
            **{f"ocupacio_{c}_percent": str(pct) for c in ("mi", "ri", "m1", "m2")}}


def test_occupancy(tmp_path):
    root = tmp_path / "collect"
    # Tue 27 Oct 2026, 07:10 UTC = 08:10 Madrid (CET)
    snapshot(root, datetime(2026, 10, 27, 7, 10), [train("S1", "a", 40), train("S1", "b", 60), train("L6", "c", 10)])
    snapshot(root, datetime(2026, 10, 27, 7, 12), [train("S1", "a", 50), train("S1", "a", 50)])  # duplicate train
    # Sat 31 Oct 2026, 11:00 UTC = 12:00 Madrid
    snapshot(root, datetime(2026, 10, 31, 11, 0), [train("S1", "a", 20)])
    # the feed sometimes shifts fields between keys; parse_fgc can't read it, the loader skips it
    snapshot(root, datetime(2026, 10, 31, 2, 0), [{"ocupacio_mi_percent": "6c4b|68", "ocupacio_mi_tram": "S2"}])

    obs = schemas.check(rs.load_occupancy(root), schemas.OCCUPANCY_OBSERVATIONS)
    assert len(obs) == 5
    summ = schemas.check(rs.occupancy_summary(obs), rs.OCCUPANCY_HOURLY).set_index(["line_id", "day_type", "hour"])
    wk = summ.loc[("fgc:S1", "weekday", 8)]
    assert wk.mean_occupancy_pct == pytest.approx((40 + 60 + 50) / 3)
    assert wk.observations == 3 and wk.days == 1
    assert summ.loc[("fgc:S1", "weekend", 12)].mean_occupancy_pct == 20
    assert ("fgc:L6", "weekday", 8) in summ.index


def test_map_to_network():
    df = schemas.RIDERSHIP.empty()
    df = pd.concat([df, pd.DataFrame({
        "period_start": pd.to_datetime(["2024-01-01"] * 4),
        "period": "month", "statistic": "total",
        "line_id": ["tmb_name:L3", "tmb_name:L3", "fgc_name:Barcelona-Vallès", "tmb_name:116"],
        "station_id": ["tmb_name:PASSEIG DE GRÀCIA", "tmb_name:LICEU", "fgc_name:Pl. Catalunya", None],
        "passengers": [1.0, 2.0, 3.0, 4.0], "source": "x",
    })]).astype({c.name: c.dtype for c in schemas.RIDERSHIP.columns if c.dtype != schemas.DATE})
    stations = pd.DataFrame({
        "station_id": ["tmb:1.326", "tmb:2.100", "fgc:PC", "tmb:1.999"],
        "name": ["Passeig de Gràcia", "Passeig de Gràcia", "Plaça Catalunya", "Liceu"],
        "mode": ["metro", "bus", "fgc", "metro"],
    })
    lines = pd.DataFrame({"line_id": ["tmb:3", "tmb:116"], "code": ["L3", "116"]})
    out, mapping = rs.map_to_network(df, stations, lines)
    assert list(out.station_id[:3]) == ["tmb:1.326", "tmb:1.999", "fgc:PC"]  # bus stop of the same name ignored
    assert list(out.line_id) == ["tmb:3", "tmb:3", "fgc_name:Barcelona-Vallès", "tmb:116"]
    schemas.check(out, schemas.RIDERSHIP)
