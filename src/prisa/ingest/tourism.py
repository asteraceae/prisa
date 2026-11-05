"""Load the Telefónica tourism files (T1-T9) into their tables.

Column names follow "Descripción de los Datasets actualizada" (14 Oct 2026),
linked from the challenge page. Files are recognised by name, e.g.
visitantes_unicos_barrio_date_dt.csv -> T5 -> presence_daily. The delivery has
one folder per month (202301/ ... 202508/) holding all nine files.

Found in the real files (Nov 2026), beyond the spec:
  * separator is ";" (detected per file);
  * age bands are written "1829", "3039"... and stored as "18-29";
  * groups under 15 people are pooled into remainder rows with "NA" even in
    key columns (barri, day, origen, tipo_visitante). Those rows can't be
    placed, so they are split off as residuals and only their size is kept
    (see `load`); "NA" in a descriptive column (country, gender...) becomes
    ANONYMISED and the row stays.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pandas as pd

from prisa import geo, schemas
from prisa.enums import SEGMENT_FROM_RAW, VISITOR_TYPE_FROM_RAW, Segment

# file stem -> (table, segment fixed by the file or None when an `origen` column says it)
FILES: dict[str, tuple[schemas.Table, Segment | None]] = {
    "visitantes_unicos_municipio_yearmon": (schemas.PRESENCE_MONTHLY, None),  # T1
    "visitantes_unicos_municipio_date_dt": (schemas.PRESENCE_DAILY, None),  # T2
    "pernoctaciones_semana": (schemas.OVERNIGHT_STAYS, None),  # T3
    "visitantes_unicos_barrio_yearmon": (schemas.PRESENCE_MONTHLY, None),  # T4
    "visitantes_unicos_barrio_date_dt": (schemas.PRESENCE_DAILY, None),  # T5
    "visitantes_regionales_barrio_yearmon": (schemas.VISITOR_ORIGIN, Segment.REGIONAL),  # T6
    "visitantes_nacionales_barrio_yearmon": (schemas.VISITOR_ORIGIN, Segment.NATIONAL),  # T7
    "visitantes_internacionales_barrio_yearmon": (schemas.VISITOR_ORIGIN, Segment.INTERNATIONAL),  # T8
    "visitantes_sociodemografico_barrio_yearmon": (schemas.VISITOR_PROFILE, None),  # T9
}

_NA = {"", "NA", "N/A", "nan", "NaN", "None", "<NA>"}


def identify(path: Path) -> str | None:
    """The FILES key a file belongs to, matched on the start of its name."""
    stem = Path(path).name.split(".")[0].lower()
    return next((k for k in sorted(FILES, key=len, reverse=True) if stem.startswith(k)), None)


def _norm(col: str) -> str:
    # "Date_dt" -> "date_dt", "pais_Residencia" -> "pais_residencia", "año" kept
    return unicodedata.normalize("NFC", col.strip().lower())


def read_raw(path: Path, **read_kwargs) -> pd.DataFrame:
    path = Path(path)
    if path.suffix == ".parquet":
        df = pd.read_parquet(path, **read_kwargs).astype(str)
    else:
        with open(path, encoding="utf-8-sig") as f:
            header = f.readline()
        sep = ";" if header.count(";") > header.count(",") else ","
        df = pd.read_csv(path, dtype=str, keep_default_na=False, sep=sep, encoding="utf-8-sig", **read_kwargs)
    return df.rename(columns=_norm)


# columns that place a row; "NA" in any of them makes it a residual
_PLACING = ["date_dt", "cod_barrio", "cod_municipio", "origen", "tipo_visitante"]


def residual_mask(raw: pd.DataFrame) -> pd.Series:
    cols = [c for c in _PLACING if c in raw.columns]
    return raw[cols].apply(lambda s: s.str.strip().isin(_NA)).any(axis=1)


def _age_band(s: pd.Series) -> pd.Series:
    s = s.str.strip()
    return s.where(~s.str.fullmatch(r"\d{4}"), s.str[:2] + "-" + s.str[2:])


def _require(df: pd.DataFrame, cols: list[str], name: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: missing columns {missing}; found {list(df.columns)}")


def _year_month(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    mes = df["mes"].str.strip()
    return pd.to_numeric(mes.str[:4]).astype("Int64"), pd.to_numeric(mes.str[4:6]).astype("Int64")


def _zone(df: pd.DataFrame) -> pd.Series:
    if "cod_barrio" in df.columns:
        return geo.zone_id_series(df["cod_municipio"], df["cod_barrio"])
    return geo.zone_id_series(df["cod_municipio"])


def _label(s: pd.Series) -> pd.Series:
    """Dimension values; Telefónica's NA (pooled groups under 15) becomes ANONYMISED."""
    s = s.str.strip()
    return s.where(~s.isin(_NA), schemas.ANONYMISED).astype("string")


def _mapped(s: pd.Series, mapping: dict, what: str) -> pd.Series:
    out = s.str.strip().map(mapping)
    bad = s[out.isna()].unique()
    if len(bad):
        raise ValueError(f"unknown {what} values: {list(bad)[:5]}")
    return out.map(str).astype("string")  # StrEnum members print as their value


def _count(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.str.strip()).astype("Int64")


def to_table(raw: pd.DataFrame, key: str) -> pd.DataFrame:
    """Rows that can be placed, in the table's contract (residuals are dropped here)."""
    table, fixed_segment = FILES[key]
    raw = raw.loc[~residual_mask(raw)]
    year, month = _year_month(raw)
    if fixed_segment is not None:
        segment = pd.Series(fixed_segment.value, index=raw.index, dtype="string")
    else:
        segment = _mapped(raw["origen"], SEGMENT_FROM_RAW, "origen")
    vtype = (
        _mapped(raw["tipo_visitante"], VISITOR_TYPE_FROM_RAW, "tipo_visitante")
        if "tipo_visitante" in raw.columns
        else None
    )
    zone = _zone(raw)

    if table is schemas.PRESENCE_DAILY:
        day_col = "date_dt"
        _require(raw, [day_col, "cod_municipio", "origen", "tipo_visitante", "visitantes"], key)
        out = {"day": pd.to_datetime(raw[day_col].str.strip()), "zone_id": zone, "segment": segment,
               "visitor_type": vtype, "visitors": _count(raw["visitantes"])}  # fmt: skip
    elif table is schemas.PRESENCE_MONTHLY:
        _require(raw, ["mes", "cod_municipio", "origen", "tipo_visitante", "visitantes"], key)
        out = {"year": year, "month": month, "zone_id": zone, "segment": segment,
               "visitor_type": vtype, "visitors": _count(raw["visitantes"])}  # fmt: skip
    elif table is schemas.OVERNIGHT_STAYS:
        _require(raw, ["mes", "semana", "periodo", "cod_municipio", "origen", "pernoctaciones", "visitantes"], key)
        out = {"year": year, "month": month, "week": _count(raw["semana"]),
               "period": raw["periodo"].str.strip().astype("string"), "zone_id": zone, "segment": segment,
               "nights": _count(raw["pernoctaciones"]), "visitors": _count(raw["visitantes"])}  # fmt: skip
    elif table is schemas.VISITOR_ORIGIN:
        name_col = {
            Segment.REGIONAL: "municipio_residencia",
            Segment.NATIONAL: "provincia_residencia",
            Segment.INTERNATIONAL: "pais_residencia",
        }[fixed_segment]
        _require(raw, ["mes", "cod_barrio", "cod_municipio", name_col, "tipo_visitante", "visitantes"], key)
        if "cod_municipio_residencia" in raw.columns:
            code = raw["cod_municipio_residencia"].str.strip()
            code = code.where(code.isin(_NA), code.map(lambda c: geo.municipality_code(c) if c.isdigit() else c))
            origin_code = code.where(~code.isin(_NA), pd.NA).astype("string")
        else:
            origin_code = pd.Series(pd.NA, index=raw.index, dtype="string")
        out = {"year": year, "month": month, "zone_id": zone, "segment": segment, "visitor_type": vtype,
               "origin_code": origin_code, "origin_name": _label(raw[name_col]),
               "visitors": _count(raw["visitantes"])}  # fmt: skip
    elif table is schemas.VISITOR_PROFILE:
        _require(raw, ["mes", "cod_barrio", "origen", "tipo_visitante", "genero", "edad", "nivel_renta", "visitantes"], key)
        out = {"year": year, "month": month, "zone_id": zone, "segment": segment, "visitor_type": vtype,
               "gender": _label(raw["genero"]), "age_band": _label(_age_band(raw["edad"])),
               "income_level": _label(raw["nivel_renta"]), "visitors": _count(raw["visitantes"])}  # fmt: skip
    else:  # pragma: no cover
        raise AssertionError(table.name)

    df = pd.DataFrame(out)
    df["zone_id"] = df["zone_id"].astype("string")
    return schemas.check(df[table.column_names], table)


def load(path: Path, **read_kwargs) -> tuple[schemas.Table, pd.DataFrame, dict]:
    """(table, rows, residual summary) for one tourism file."""
    key = identify(path)
    if key is None:
        raise ValueError(f"{Path(path).name}: not a known tourism file ({', '.join(FILES)})")
    raw = read_raw(path, **read_kwargs)
    res = raw.loc[residual_mask(raw)]
    residual = {
        "file": key,
        "rows": len(res),
        "visitors": int(pd.to_numeric(res["visitantes"]).sum()) if len(res) else 0,
        "total_visitors": int(pd.to_numeric(raw["visitantes"]).sum()),
    }
    return FILES[key][0], to_table(raw, key), residual
