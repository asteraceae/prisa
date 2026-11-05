"""Daily summary tables built from the Telefónica data: `barri_day` and `city_day`.

    uv run python -m prisa.features

Trips per barri use M1 (barri <-> barri) plus M3 (barri <-> other
municipality) and never M2, which repeats the M3 trips at municipality level.
City totals into and out of Barcelona use M2, which is complete at that
level (M3 loses some to censoring). Visitor counts are tourism T5 (barri) and
T2 (city); they are unique people, summed only across segments and visitor
types, never across days or barris.
"""

from __future__ import annotations

import sys

import duckdb
import pandas as pd

from prisa import geo, schemas, store
from prisa.enums import Segment, VisitorType

_BARRI = f"length({{col}}) = {geo.BARRI_LEN}"


def _seg_sums(prefix: str) -> str:
    return ",\n".join(
        f"sum(trips) FILTER (WHERE segment = '{s.value}')::DOUBLE AS {prefix}_{s.value}" for s in Segment
    )


def barri_day_sql() -> str:
    return f"""
    WITH flows AS (
        SELECT day, origin_zone_id, dest_zone_id, segment, trips::DOUBLE AS trips
        FROM trip_flows WHERE source IN ('barri_barri', 'barri_muni')
    ),
    inbound AS (
        SELECT day, dest_zone_id AS zone_id, sum(trips) AS trips_in, {_seg_sums("trips_in")}
        FROM flows WHERE {_BARRI.format(col="dest_zone_id")} GROUP BY ALL
    ),
    outbound AS (
        SELECT day, origin_zone_id AS zone_id, sum(trips) AS trips_out
        FROM flows WHERE {_BARRI.format(col="origin_zone_id")} GROUP BY ALL
    ),
    visitors AS (
        SELECT day, zone_id,
            sum(visitors)::DOUBLE AS visitors,
            sum(visitors) FILTER (WHERE segment = '{Segment.INTERNATIONAL}')::DOUBLE AS visitors_international,
            sum(visitors) FILTER (WHERE segment = '{Segment.NATIONAL}')::DOUBLE AS visitors_national,
            sum(visitors) FILTER (WHERE segment = '{Segment.REGIONAL}')::DOUBLE AS visitors_regional,
            sum(visitors) FILTER (WHERE visitor_type = '{VisitorType.TOURIST}')::DOUBLE AS tourists,
            sum(visitors) FILTER (WHERE visitor_type = '{VisitorType.DAY_TRIPPER}')::DOUBLE AS day_trippers
        FROM presence_daily WHERE {_BARRI.format(col="zone_id")} GROUP BY ALL
    )
    SELECT i.day, i.zone_id, i.trips_in,
        {", ".join(f"coalesce(i.trips_in_{s.value}, 0) AS trips_in_{s.value}" for s in Segment)},
        coalesce(o.trips_out, 0) AS trips_out,
        v.visitors, v.visitors_international, v.visitors_national, v.visitors_regional,
        v.tourists, v.day_trippers
    FROM inbound i
    LEFT JOIN outbound o USING (day, zone_id)
    LEFT JOIN visitors v USING (day, zone_id)
    ORDER BY day, zone_id
    """


def city_day_sql() -> str:
    bcn = geo.BARCELONA
    return f"""
    WITH v AS (
        SELECT day,
            sum(visitors)::DOUBLE AS visitors,
            sum(visitors) FILTER (WHERE segment = '{Segment.INTERNATIONAL}')::DOUBLE AS visitors_international,
            sum(visitors) FILTER (WHERE segment = '{Segment.NATIONAL}')::DOUBLE AS visitors_national,
            sum(visitors) FILTER (WHERE segment = '{Segment.REGIONAL}')::DOUBLE AS visitors_regional,
            sum(visitors) FILTER (WHERE visitor_type = '{VisitorType.TOURIST}')::DOUBLE AS tourists,
            sum(visitors) FILTER (WHERE visitor_type = '{VisitorType.DAY_TRIPPER}')::DOUBLE AS day_trippers
        FROM presence_daily WHERE zone_id = '{bcn}' GROUP BY day
    ),
    internal AS (
        SELECT day, sum(trips)::DOUBLE AS trips_internal,
            sum(trips) FILTER (WHERE segment <> '{Segment.RESIDENT}')::DOUBLE AS trips_internal_visitor
        FROM trip_flows WHERE source = 'barri_barri' GROUP BY day
    ),
    muni AS (
        SELECT day,
            sum(trips) FILTER (WHERE dest_zone_id = '{bcn}')::DOUBLE AS trips_into_city,
            sum(trips) FILTER (WHERE origin_zone_id = '{bcn}')::DOUBLE AS trips_out_of_city
        FROM trip_flows WHERE source = 'muni_muni' GROUP BY day
    )
    SELECT v.day, v.visitors, v.visitors_international, v.visitors_national, v.visitors_regional,
        v.tourists, v.day_trippers, i.trips_internal, m.trips_into_city, m.trips_out_of_city,
        i.trips_internal_visitor
    FROM v JOIN internal i USING (day) JOIN muni m USING (day)
    ORDER BY day
    """


def _frame(con: duckdb.DuckDBPyConnection, sql: str, table: schemas.Table) -> pd.DataFrame:
    df = con.execute(sql).df()
    df["day"] = pd.to_datetime(df["day"])
    casts = {c.name: c.dtype for c in table.columns if c.dtype != schemas.DATE}
    return schemas.check(df.astype(casts)[table.column_names], table)


def build(con: duckdb.DuckDBPyConnection | None = None) -> dict[str, pd.DataFrame]:
    con = con or store.connect()
    out = {
        "barri_day": _frame(con, barri_day_sql(), schemas.BARRI_DAY),
        "city_day": _frame(con, city_day_sql(), schemas.CITY_DAY),
    }
    for name, df in out.items():
        store.write(df, name)
    return out


def main() -> int:
    for name, df in build().items():
        print(f"{name}: {len(df):,} rows, {df['day'].min():%Y-%m-%d} to {df['day'].max():%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
