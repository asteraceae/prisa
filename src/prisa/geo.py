"""Zone identifiers.

A zone is either a Barcelona barri or a municipality of the province. Both
live in the same `zone_id` column, told apart by length:

    080190103  barri: 08019 (Barcelona) + 01 (district) + 03 (barri within district)
    08019      municipality: 5-digit INE code, leading zero kept

This is the format of the real Telefónica delivery (Nov 2026). The spec PDF
showed 8-digit codes (80190103) without the leading zero; those are accepted
and padded. The last two digits restart in every district, so they are NOT the
official city-wide barri number (1-73): el Bon Pastor is 080190903 here and
barri 59 officially. `zones.barri_number` holds the official number.

Read every code as a string: a numeric read turns 08019 into 8019 and joins
against INE or Open Data BCN fail silently.
"""

from __future__ import annotations

import pandas as pd

from prisa.enums import ZoneLevel

BARCELONA = "08019"
BARRI_PREFIX = "08019"
BARRI_LEN = 9
MAX_BARRIS_PER_DISTRICT = 13  # Nou Barris
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
    if s.isdigit() and len(s) == BARRI_LEN - 1:  # spec-PDF form without the leading zero
        s = "0" + s
    if not (s.isdigit() and len(s) == BARRI_LEN and s.startswith(BARRI_PREFIX)):
        raise ValueError(f"not a Barcelona barri code: {raw!r}")
    return s


def zone_id(municipality: object, barri: object = None) -> str:
    """The finest zone a row refers to: the barri if there is one, else the municipality."""
    b = barri_code(barri) if barri is not None else None
    return b if b is not None else municipality_code(municipality)


def zone_level(zid: str) -> ZoneLevel:
    return ZoneLevel.BARRI if len(zid) == BARRI_LEN else ZoneLevel.MUNICIPALITY


def district_of(barri: str) -> int:
    return int(barri[5:7])


def barri_in_district(barri: str) -> int:
    """Position of the barri inside its district (the code's last two digits)."""
    return int(barri[7:9])


def municipality_of(zid: str) -> str:
    return BARCELONA if len(zid) == BARRI_LEN else zid


def infer_barri_numbering(codes: pd.Series) -> str:
    """Tell whether the last two digits are the official city-wide number (1-73) or restart per district.

    No district has more than 13 barris (Nou Barris), so a position above 13
    can only be a city-wide number. Ciutat Vella (01-04) fits both schemes,
    so codes from district 01 alone can't decide.
    """
    codes = pd.Series(codes).dropna().astype(str).unique()
    if not len(codes) or all(district_of(c) == 1 for c in codes):
        return "unknown"
    return "city_wide" if any(barri_in_district(c) > MAX_BARRIS_PER_DISTRICT for c in codes) else "per_district"


# Vectorised versions for whole columns


def zone_id_series(municipality: pd.Series, barri: pd.Series | None = None) -> pd.Series:
    muni = municipality.map(municipality_code)
    if barri is None:
        return muni.astype("string")
    b = barri.map(barri_code)
    return b.where(b.notna(), muni).astype("string")


def zone_level_series(zids: pd.Series) -> pd.Series:
    return zids.str.len().map({BARRI_LEN: ZoneLevel.BARRI.value, 5: ZoneLevel.MUNICIPALITY.value}).astype("string")
