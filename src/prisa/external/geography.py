"""Barcelona geography, population and tourist-flat reference tables.

    uv run python -m prisa.external.geography

Downloads what is missing into data/raw/external/geography/ and writes
zones, districts, zone_population, zone_accommodation and barri_geometry to
data/processed/, plus the committed crosswalk data/reference/barri_crosswalk.csv.

Sources (all Open Data BCN, CC BY 4.0, "Ajuntament de Barcelona"; downloaded
2026-11-05 through the CKAN API, package_show?id=<dataset>):
  20170706-districtes-barris  district and barri boundaries, WKT in ETRS89
      (EPSG:25831, area from this) and WGS84
      https://opendata-ajuntament.barcelona.cat/data/dataset/20170706-districtes-barris
  pad_mdbas_sexe  padró residents per census section and sex at 1 January
      https://opendata-ajuntament.barcelona.cat/data/dataset/pad_mdbas_sexe
  habitatges-us-turistic  tourist-flat (HUT) register with places, quarterly
      https://opendata-ajuntament.barcelona.cat/data/dataset/habitatges-us-turistic
Not used: allotjaments-hotels lists hotel points without places, so
hotel_beds stays null.

AMB membership: the 36 municipalities listed at
https://www.amb.cat/web/area-metropolitana/municipis-metropolitans (checked
2026-11-05). Province municipalities and their INE codes come from the
Telefónica municipios mobility files (read locally, never redistributed).

Telefónica numbers barris by position inside their district (see prisa.geo);
the crosswalk ranks the official 1-73 numbers inside each district and then
checks every name against Telefónica's own barri names.

tourist_flat_beds is the mean of the year's quarterly HUT snapshots (2024 Q2
has no places column and is skipped; 2024 = Q3-Q4, 2026 = Q1 and Q3; flats
with no places count as 0). Barcelona's area is the sum of its barris; other
municipalities have no area.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import shapely
from shapely import wkt

from prisa import geo, reference, schemas
from prisa.enums import ZoneLevel

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / "external" / "geography"
TELEFONICA_DIR = ROOT / "data" / "raw" / "telefonica" / "movilidad"
PROCESSED_DIR = ROOT / "data" / "processed"
CROSSWALK_PATH = reference.REFERENCE_DIR / "barri_crosswalk.csv"

CKAN = "https://opendata-ajuntament.barcelona.cat/data/api/3/action/package_show?id="
BOUNDARIES = "20170706-districtes-barris"
PADRO = "pad_mdbas_sexe"
HUT = "habitatges-us-turistic"
YEARS = range(2023, 2027)

# slugs as on amb.cat/web/area-metropolitana/municipis-metropolitans
AMB_SLUGS = frozenset({
    "badalona", "badia-del-valles", "barbera-del-valles", "barcelona", "begues", "castellbisbal",
    "castelldefels", "cerdanyola-del-valles", "cervello", "corbera-de-llobregat",
    "cornella-de-llobregat", "el-papiol", "el-prat-de-llobregat", "esplugues-de-llobregat", "gava",
    "la-palma-de-cervello", "l-hospitalet-de-llobregat", "molins-de-rei", "montcada-i-reixac",
    "montgat", "palleja", "ripollet", "santa-coloma-de-cervello", "santa-coloma-de-gramenet",
    "sant-adria-de-besos", "sant-andreu-de-la-barca", "sant-boi-de-llobregat",
    "sant-climent-de-llobregat", "sant-cugat-del-valles", "sant-feliu-de-llobregat",
    "sant-joan-despi", "sant-just-desvern", "sant-vicenc-dels-horts", "tiana",
    "torrelles-de-llobregat", "viladecans",
})  # fmt: skip

CROSSWALK_COLUMNS = ["zone_id", "barri_number", "barri_name", "district_code", "district_name"]

BARRI_GEOMETRY = schemas.Table(
    "barri_geometry",
    "dimension",
    ("zone_id",),
    (
        schemas.C("zone_id", schemas.STR),
        schemas.C("wkt_wgs84", schemas.STR, "POLYGON / MULTIPOLYGON, lon lat order"),
        schemas.C("centroid_lat", schemas.FLOAT),
        schemas.C("centroid_lon", schemas.FLOAT),
    ),
    "Barri boundaries for point-in-polygon joins (shapely.from_wkt).",
    sources=("Open Data BCN districtes-barris",),
)


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "prisa (UPF Mobile Data Challenge)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _resources(package: str) -> dict[str, str]:
    """Resource name -> download URL for an Open Data BCN dataset."""
    meta = json.loads(_get(CKAN + package))["result"]
    return {r["name"].strip(): r["url"] for r in meta["resources"]}


def _wanted(package: str, name: str) -> bool:
    if package == BOUNDARIES:
        return name in ("BarcelonaCiutat_Barris.csv", "BarcelonaCiutat_Districtes.csv")
    if package == PADRO:
        m = re.fullmatch(r"(\d{4})_pad_mdbas_sexe\.csv", name)
        return bool(m) and int(m[1]) in YEARS
    if package == HUT:
        m = re.match(r"(\d{4})_\dT_hut_comunicacio", name)
        return bool(m) and name.endswith(".zip") and int(m[1]) in YEARS
    return False


def download(raw_dir: Path = RAW_DIR) -> None:
    """Fetch every wanted resource that is not on disk yet. Offline runs reuse what is there."""
    for package in (BOUNDARIES, PADRO, HUT):
        target = raw_dir / package
        target.mkdir(parents=True, exist_ok=True)
        try:
            resources = _resources(package)
        except OSError as e:
            if not any(target.iterdir()):
                raise
            print(f"{package}: CKAN unreachable ({e}); using local files", file=sys.stderr)
            continue
        for name, url in sorted(resources.items()):
            path = target / name
            if _wanted(package, name) and not path.exists():
                print(f"downloading {package}/{name}", file=sys.stderr)
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(_get(url))
                tmp.rename(path)


# --------------------------------------------------------------------------
# Names and crosswalk
# --------------------------------------------------------------------------

_ARTICLES = {"el", "la", "les", "els", "l"}


def normalise_name(name: str) -> str:
    """Accent-, case-, punctuation- and article-insensitive key: "Sant Pere, Santa Caterina i la Ribera"
    and "SANT PERE SANTA CATERINA I LA RIBERA" match, as do "Sants - Badal" and "Sants-Badal"."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", s).split()
    return " ".join(w for w in words if w not in _ARTICLES)


def slug(name: str) -> str:
    """amb.cat-style slug; INE's "Prat de Llobregat, El" becomes el-prat-de-llobregat."""
    if m := re.fullmatch(r"(.+), (El|La|Les|Els|L')", name.strip()):
        name = f"{m[2]} {m[1]}"
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def build_crosswalk(barris: pd.DataFrame) -> pd.DataFrame:
    """Official (district, barri number, names) -> Telefónica zone_id by rank inside the district.

    `barris` needs district_code, district_name, barri_number, barri_name.
    """
    df = barris.sort_values(["district_code", "barri_number"]).reset_index(drop=True)
    pos = df.groupby("district_code").cumcount() + 1
    df["zone_id"] = [f"{geo.BARRI_PREFIX}{d:02d}{p:02d}" for d, p in zip(df["district_code"], pos)]
    out = df[CROSSWALK_COLUMNS].astype(
        {"zone_id": "string", "barri_number": "Int64", "barri_name": "string",
         "district_code": "Int64", "district_name": "string"}
    )  # fmt: skip
    if out["zone_id"].duplicated().any() or out["barri_number"].duplicated().any():
        raise ValueError("crosswalk: duplicate zone_id or barri_number")
    return out


def verify_crosswalk(crosswalk: pd.DataFrame, telefonica_names: dict[str, str]) -> None:
    """Fail unless every barri maps one-to-one onto a Telefónica code with the same name."""
    ours = dict(zip(crosswalk["zone_id"], crosswalk["barri_name"]))
    problems = [f"{z}: in Telefónica, not in crosswalk ({n})" for z, n in telefonica_names.items() if z not in ours]
    for z, name in ours.items():
        theirs = telefonica_names.get(z)
        if theirs is None:
            problems.append(f"{z}: in crosswalk ({name}), not in Telefónica")
        elif normalise_name(theirs) != normalise_name(name):
            problems.append(f"{z}: official {name!r} vs Telefónica {theirs!r}")
    if problems:
        raise ValueError("barri crosswalk does not match Telefónica:\n  " + "\n  ".join(problems))


def load_crosswalk(path: Path = CROSSWALK_PATH) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.astype(
        {"zone_id": "string", "barri_number": "Int64", "barri_name": "string",
         "district_code": "Int64", "district_name": "string"}
    )[CROSSWALK_COLUMNS]  # fmt: skip


def _telefonica(pattern: str, code: str, parse) -> dict[str, str]:
    """Distinct (code, name) pairs across every origin and destination column of the matching files."""
    files = sorted(TELEFONICA_DIR.rglob(pattern))
    if not files:
        return {}
    src = f"read_csv({[str(f) for f in files]}, all_varchar=true)"
    con = duckdb.connect()
    con.execute("set enable_progress_bar = false")
    rows = con.execute(
        f"select distinct {code}_origen, {code}_origen_name from {src} "
        f"union select distinct {code}_destino, {code}_destino_name from {src}"
    ).fetchall()
    out: dict[str, str] = {}
    for c, n in rows:
        if c is None or c.strip() in geo.MISSING:
            continue
        c = parse(c)
        if out.setdefault(c, n) != n:
            raise ValueError(f"Telefónica uses two names for {c}: {out[c]!r}, {n!r}")
    return out


def telefonica_barris() -> dict[str, str]:
    return _telefonica("movilidad_barrios_*_origen.csv", "barrio", geo.barri_code)


def telefonica_municipalities() -> dict[str, str]:
    return _telefonica("movilidad_municipios_*_origen.csv", "municipio", geo.municipality_code)


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def area_km2(wkt_etrs89: str) -> float:
    """Planar area of a WKT polygon in a metric CRS (ETRS89 / UTM 31N), in km2."""
    return shapely.area(_valid(wkt.loads(wkt_etrs89))) / 1e6


def _valid(geom):
    return geom if geom.is_valid else shapely.make_valid(geom)


def read_boundaries(raw_dir: Path = RAW_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(barris, districts) with integer codes, official names and both WKTs."""
    folder = raw_dir / BOUNDARIES
    b = pd.read_csv(folder / "BarcelonaCiutat_Barris.csv", dtype=str)
    d = pd.read_csv(folder / "BarcelonaCiutat_Districtes.csv", dtype=str).rename(columns=str.lower)
    barris = pd.DataFrame({
        "district_code": b["codi_districte"].astype(int),
        "district_name": b["nom_districte"].str.strip(),
        "barri_number": b["codi_barri"].astype(int),
        "barri_name": b["nom_barri"].str.strip(),
        "wkt_etrs89": b["geometria_etrs89"],
        "wkt_wgs84": b["geometria_wgs84"],
    })  # fmt: skip
    districts = pd.DataFrame({
        "district_code": d["codi_districte"].astype(int),
        "name": d["nom_districte"].str.strip(),
        "wkt_etrs89": d["geometria_etrs89"],
    })  # fmt: skip
    if len(barris) != 73 or len(districts) != 10:
        raise ValueError(f"expected 73 barris and 10 districts, got {len(barris)} and {len(districts)}")
    return barris, districts


def barri_geometry(barris: pd.DataFrame, crosswalk: pd.DataFrame) -> pd.DataFrame:
    df = barris.merge(crosswalk[["barri_number", "zone_id"]], on="barri_number", validate="1:1")
    geoms = [_valid(wkt.loads(w)) for w in df["wkt_wgs84"]]
    lon0, lat0, lon1, lat1 = shapely.bounds(geoms).T
    if not ((lon0 > 2.0) & (lon1 < 2.3) & (lat0 > 41.3) & (lat1 < 41.5)).all():
        raise ValueError("barri WGS84 geometry is not in lon lat order around Barcelona")
    centroids = shapely.centroid(geoms)
    out = pd.DataFrame({
        "zone_id": df["zone_id"].astype("string"),
        "wkt_wgs84": pd.Series(shapely.to_wkt(geoms, rounding_precision=7), dtype="string"),
        "centroid_lat": shapely.get_y(centroids).astype("float64"),
        "centroid_lon": shapely.get_x(centroids).astype("float64"),
    }).sort_values("zone_id", ignore_index=True)  # fmt: skip
    return schemas.check(out, BARRI_GEOMETRY)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def districts_table(districts: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "district_code": districts["district_code"].astype("Int64"),
        "name": districts["name"].astype("string"),
        "area_km2": districts["wkt_etrs89"].map(area_km2).astype("float64"),
    }).sort_values("district_code", ignore_index=True)  # fmt: skip
    return schemas.check(out, schemas.DISTRICTS)


def zones_table(barris: pd.DataFrame, crosswalk: pd.DataFrame, municipalities: dict[str, str]) -> pd.DataFrame:
    """73 barris plus every municipality in `municipalities` (INE code -> name)."""
    b = barris.merge(crosswalk[["barri_number", "zone_id"]], on="barri_number", validate="1:1")
    barri_rows = pd.DataFrame({
        "zone_id": b["zone_id"],
        "level": ZoneLevel.BARRI.value,
        "name": b["barri_name"],
        "municipality_code": geo.BARCELONA,
        "district_code": b["district_code"],
        "barri_number": b["barri_number"],
        "area_km2": b["wkt_etrs89"].map(area_km2),
        "in_amb": True,
    })  # fmt: skip

    municipalities = {geo.BARCELONA: "Barcelona", **municipalities}
    amb = {code for code, name in municipalities.items() if slug(name) in AMB_SLUGS}
    found = {slug(municipalities[c]) for c in amb}
    if found != AMB_SLUGS:
        raise ValueError(f"AMB municipalities not found by name: {sorted(AMB_SLUGS - found)}")
    muni_rows = pd.DataFrame({
        "zone_id": list(municipalities),
        "level": ZoneLevel.MUNICIPALITY.value,
        "name": list(municipalities.values()),
        "municipality_code": list(municipalities),
        "district_code": pd.NA,
        "barri_number": pd.NA,
        "area_km2": [barri_rows["area_km2"].sum() if c == geo.BARCELONA else None for c in municipalities],
        "in_amb": [c in amb for c in municipalities],
    })  # fmt: skip

    out = pd.concat([barri_rows, muni_rows], ignore_index=True)
    out["atm_fare_zone"] = None
    out = out.astype({c.name: c.dtype for c in schemas.ZONES.columns})
    return schemas.check(out.sort_values("zone_id", ignore_index=True), schemas.ZONES)


def population_table(crosswalk: pd.DataFrame, raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Padró residents per barri at 1 January, plus the Barcelona total."""
    parts = []
    for year in YEARS:
        p = pd.read_csv(raw_dir / PADRO / f"{year}_pad_mdbas_sexe.csv", dtype=str)
        ref_year = pd.to_datetime(p["Data_Referencia"]).dt.year.unique()
        if list(ref_year) != [year]:
            raise ValueError(f"padró {year}: Data_Referencia years {ref_year}")
        p = p.assign(barri_number=p["Codi_Barri"].astype(int), population=pd.to_numeric(p["Valor"]))
        parts.append(p.groupby("barri_number", as_index=False)["population"].sum().assign(year=year))
    pop = pd.concat(parts).merge(crosswalk[["barri_number", "zone_id"]], on="barri_number", how="left")
    if pop["zone_id"].isna().any() or (pop.groupby("year").size() != 73).any():
        raise ValueError("padró: barri numbers outside the 73 official barris")
    total = pop.groupby("year", as_index=False)["population"].sum().assign(zone_id=geo.BARCELONA)
    out = pd.concat([pop, total])[["zone_id", "year", "population"]]
    out = out.astype({"zone_id": "string", "year": "Int64", "population": "Int64"})
    return schemas.check(out.sort_values(["year", "zone_id"], ignore_index=True), schemas.ZONE_POPULATION)


def read_hut(path: Path) -> pd.DataFrame | None:
    """One quarterly HUT snapshot, one row per registered flat; None if it has no places column."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(next(n for n in z.namelist() if n.lower().endswith(".csv")))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"LATITUD_Y(?!\s)", "LATITUD_Y\n", text, count=1)  # 2023 Q1/Q3: header glued to row 1
    header, *rows = csv.reader(io.StringIO(text))
    header = [h.strip().upper() for h in header]
    if "NUMERO_PLACES" not in header:
        return None
    if header[3] != "CODI_BARRI" or header[-4:] != ["NUMERO_REGISTRE_GENERALITAT", "NUMERO_PLACES", "LONGITUD_X", "LATITUD_Y"]:
        raise ValueError(f"{path.name}: unexpected columns {header}")
    # barri names like "Sant Pere, Santa Caterina i la Ribera" are sometimes unquoted,
    # so read the barri code from the left and the rest from the right
    rows = [r for r in rows if r]
    col = lambda i: pd.Series([r[i].strip() for r in rows], dtype="string")  # noqa: E731
    num = lambda i: pd.to_numeric(col(i).replace("", None), errors="coerce")  # noqa: E731
    return pd.DataFrame({
        "registry": col(-4),
        "barri_number": num(3).astype("Int64"),
        "places": num(-3).astype("Int64"),
        "lon": num(-2).astype("float64"),
        "lat": num(-1).astype("float64"),
    })  # fmt: skip


def locate(lon: pd.Series, lat: pd.Series, geometry: pd.DataFrame) -> pd.Series:
    """zone_id of the barri each WGS84 point falls in (NA outside Barcelona); `geometry` is barri_geometry."""
    polys = shapely.from_wkt(geometry["wkt_wgs84"].to_numpy(dtype=object))
    nan = float("nan")
    points = shapely.points(lon.to_numpy(dtype=float, na_value=nan), lat.to_numpy(dtype=float, na_value=nan))
    point_idx, poly_idx = shapely.STRtree(polys).query(points, predicate="intersects")
    out = pd.Series(pd.NA, index=lon.index, dtype="string")
    hits = pd.Series(poly_idx, index=point_idx)
    hits = hits[~hits.index.duplicated()]  # a point on a shared border takes the first barri
    out.iloc[hits.index] = geometry["zone_id"].to_numpy()[hits.to_numpy()]
    return out


def clean_hut(hut: pd.DataFrame, crosswalk: pd.DataFrame, geometry: pd.DataFrame) -> pd.DataFrame:
    """One row per flat with a valid barri_number.

    2023 Q4 repeats about 4,200 flats without a barri code (and mostly without places), so
    duplicates of a registry number keep the row that has a barri; flats still missing a barri
    are placed by their coordinates.
    """
    hut = hut.assign(_nobarri=hut["barri_number"].isna())
    hut = hut.sort_values("_nobarri", kind="stable")
    hut = hut[~(hut["registry"].duplicated() & hut["registry"].ne(""))].drop(columns="_nobarri")
    missing = hut["barri_number"].isna() | ~hut["barri_number"].isin(crosswalk["barri_number"])
    if missing.any():
        by_zone = dict(zip(crosswalk["zone_id"], crosswalk["barri_number"]))
        zones = locate(hut.loc[missing, "lon"], hut.loc[missing, "lat"], geometry)
        hut.loc[missing, "barri_number"] = zones.map(by_zone).astype("Int64")
    return hut[hut["barri_number"].notna()]


def accommodation_table(crosswalk: pd.DataFrame, geometry: pd.DataFrame, raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Tourist-flat places per barri and year: mean over the year's usable quarterly snapshots."""
    snaps = []
    for path in sorted((raw_dir / HUT).glob("*_hut_comunicacio*.zip")):
        year, quarter = re.match(r"(\d{4})_(\d)T", path.name).groups()
        hut = read_hut(path)
        if hut is None:
            print(f"{path.name}: no NUMERO_PLACES column, skipped", file=sys.stderr)
            continue
        flats = clean_hut(hut, crosswalk, geometry)
        if len(flats) < 0.99 * hut["registry"].replace("", None).nunique(dropna=True):
            raise ValueError(f"{path.name}: {len(hut) - len(flats)} flats could not be placed in a barri")
        sums = flats.groupby("barri_number")["places"].sum().reindex(crosswalk["barri_number"], fill_value=0)
        snaps.append(sums.rename("places").reset_index().assign(year=int(year), quarter=int(quarter)))
    if not snaps:
        raise FileNotFoundError(f"no HUT snapshots in {raw_dir / HUT}")
    yearly = pd.concat(snaps).groupby(["year", "barri_number"], as_index=False)["places"].mean()
    yearly = yearly.merge(crosswalk[["barri_number", "zone_id"]], on="barri_number")
    out = pd.DataFrame({
        "zone_id": yearly["zone_id"].astype("string"),
        "year": yearly["year"].astype("Int64"),
        "hotel_beds": pd.Series(pd.NA, index=yearly.index, dtype="Int64"),
        "tourist_flat_beds": yearly["places"].round().astype("Int64"),
    })  # fmt: skip
    return schemas.check(out.sort_values(["year", "zone_id"], ignore_index=True), schemas.ZONE_ACCOMMODATION)


def hotspots_with_zones(crosswalk: pd.DataFrame | None = None) -> pd.DataFrame:
    """data/reference/hotspots.csv with zone_id filled from barri_number."""
    crosswalk = load_crosswalk() if crosswalk is None else crosswalk
    hot = reference.load(schemas.HOTSPOTS)
    zone = hot["barri_number"].map(dict(zip(crosswalk["barri_number"], crosswalk["zone_id"])))
    unknown = hot["barri_number"].notna() & zone.isna()
    if unknown.any():
        raise ValueError(f"hotspots with unknown barri_number: {list(hot.loc[unknown, 'hotspot_id'])}")
    hot["zone_id"] = zone.astype("string")
    return schemas.check(hot, schemas.HOTSPOTS)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build(raw_dir: Path = RAW_DIR, out_dir: Path = PROCESSED_DIR) -> dict[str, pd.DataFrame]:
    download(raw_dir)
    barris, districts = read_boundaries(raw_dir)
    crosswalk = build_crosswalk(barris)

    tf_barris = telefonica_barris()
    if tf_barris:
        verify_crosswalk(crosswalk, tf_barris)
        crosswalk.to_csv(CROSSWALK_PATH, index=False)
        print(f"crosswalk verified against {len(tf_barris)} Telefónica barri names", file=sys.stderr)
    elif not crosswalk.equals(load_crosswalk()):
        raise ValueError("no Telefónica files to verify against and the crosswalk differs from the committed one")

    municipalities = telefonica_municipalities()
    if not municipalities:
        raise FileNotFoundError(f"no Telefónica municipios files under {TELEFONICA_DIR}")

    geometry = barri_geometry(barris, crosswalk)
    tables = {
        "zones": zones_table(barris, crosswalk, municipalities),
        "districts": districts_table(districts),
        "zone_population": population_table(crosswalk, raw_dir),
        "zone_accommodation": accommodation_table(crosswalk, geometry, raw_dir),
        "barri_geometry": geometry,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        print(f"{name}: {len(df)} rows", file=sys.stderr)
    return tables


if __name__ == "__main__":
    build()
