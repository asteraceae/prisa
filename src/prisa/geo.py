"""Zone identifiers.

A zone is either a Barcelona barri or a municipality of the province. Both
live in the same `zone_id` column, told apart by length:

    80190103  barri: 8019 (Barcelona) + 01 (district) + 03 (barri)
    08019     municipality: 5-digit INE code, leading zero kept

Telefónica's barri code drops the leading zero of 08019, which is why the
barri prefix is 8019. Read every code as a string: a numeric read turns
08019 into 8019 and joins against INE or Open Data BCN fail silently.
"""

from __future__ import annotations

import pandas as pd

from prisa.enums import ZoneLevel

BARCELONA = "08019"
BARRI_PREFIX = "8019"
MISSING = {"", "-", "nan", "None", "<NA>"}


def _clean(raw: object) -> str:
    s = str(raw).strip()
    if s.endswith(".0"):  # code went through a float on the way in
        s = s[:-2]
    return s


def municipality_code(raw: object) -> str:
    s = _clean(raw)
    if not s.isdigit() or len(s) > 5:
        raise ValueError(f"not an INE municipality code: {raw!r}")
    return s.zfill(5)


def barri_code(raw: object) -> str | None:
    """Normalise a barri code; '-' (used for non-Barcelona destinations) becomes None."""
    s = _clean(raw)
    if s in MISSING:
        return None
    if not (s.isdigit() and len(s) == 8 and s.startswith(BARRI_PREFIX)):
        raise ValueError(f"not a Barcelona barri code: {raw!r}")
    return s


def zone_id(municipality: object, barri: object = None) -> str:
    """The finest zone a row refers to: the barri if there is one, else the municipality."""
    b = barri_code(barri) if barri is not None else None
    return b if b is not None else municipality_code(municipality)


def zone_level(zid: str) -> ZoneLevel:
    return ZoneLevel.BARRI if len(zid) == 8 else ZoneLevel.MUNICIPALITY


def district_of(barri: str) -> int:
    return int(barri[4:6])


def municipality_of(zid: str) -> str:
    return BARCELONA if len(zid) == 8 else zid


def infer_barri_numbering(codes: pd.Series) -> str:
    """Tell whether the last two digits are the official city-wide number (1-73) or restart per district.

    Official numbering is city-wide: la Barceloneta is 03, el Fort Pienc (Eixample) is 05.
    Ciutat Vella can't tell the two apart, so look at any other district: if
    its smallest suffix is above 01, the suffix is the city-wide number.
    """
    codes = codes.dropna().astype(str).unique()
    by_district: dict[int, int] = {}
    for c in codes:
        d, n = district_of(c), int(c[6:8])
        by_district[d] = min(n, by_district.get(d, n))
    others = {d: n for d, n in by_district.items() if d != 1}
    if not others:
        return "unknown"
    return "city_wide" if any(n > 1 for n in others.values()) else "per_district"


# Vectorised versions for whole columns


def zone_id_series(municipality: pd.Series, barri: pd.Series | None = None) -> pd.Series:
    muni = municipality.map(municipality_code)
    if barri is None:
        return muni.astype("string")
    b = barri.map(barri_code)
    return b.where(b.notna(), muni).astype("string")


def zone_level_series(zids: pd.Series) -> pd.Series:
    return zids.str.len().map({8: ZoneLevel.BARRI.value, 5: ZoneLevel.MUNICIPALITY.value}).astype("string")
