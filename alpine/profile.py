"""Step 3 — profile the raw data before trusting any of it.

This step produces no pipeline output. Its entire job is to answer: *what did we actually
get?* Doing this before modelling is what separates someone who has handled real data from
someone who has read about it.

Five questions, in order:
  1. What are the columns and types?
  2. Where are the nulls?
  3. What do the distributions look like — any impossible values?
  4. Do the internally derivable fields agree? (slopes and lifts should sum to their totals)
  5. Is the text clean? (encoding damage is invisible until you look)
"""
from __future__ import annotations

import duckdb

from .config import SNOW_GRID_DEG, SNOW_GRID_OFFSET
from .seed import connect

RULE = "─" * 78


def _h(title: str) -> None:
    print(f"\n{RULE}\n  {title}\n{RULE}")


def profile(con: duckdb.DuckDBPyConnection | None = None) -> None:
    owned = con is None
    con = con or connect()
    try:
        _profile_resorts(con)
        if _has_table(con, "snow"):
            _profile_snow(con)
        else:
            print("\n  (raw.snow not loaded — snow.csv missing)")
    finally:
        if owned:
            con.close()


def _has_table(con, name: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE schema_name='raw' AND table_name=?",
        [name],
    ).fetchone()[0] > 0


# ---------------------------------------------------------------------------- resorts
def _profile_resorts(con) -> None:
    _h("1. raw.resorts — shape and types")
    print(con.execute("DESCRIBE raw.resorts").df().to_string(index=False))

    n = con.execute("SELECT count(*) FROM raw.resorts").fetchone()[0]
    print(f"\n  rows: {n:,}")

    _h("2. Nulls")
    cols = [r[0] for r in con.execute("DESCRIBE raw.resorts").fetchall()]
    parts = [f"sum(CASE WHEN \"{c}\" IS NULL THEN 1 ELSE 0 END) AS \"{c}\"" for c in cols]
    nulls = con.execute(f"SELECT {', '.join(parts)} FROM raw.resorts").df().T
    nulls.columns = ["n_null"]
    nulls = nulls[nulls["n_null"] > 0]
    print("  no nulls anywhere" if nulls.empty else nulls.to_string())

    _h("3. Numeric ranges — looking for the impossible")
    print(con.execute("""
        SELECT 'Price'          AS col, min("Price")          AS lo, max("Price")          AS hi,
               round(avg("Price"), 1) AS mean
        FROM raw.resorts
        UNION ALL SELECT 'Highest point', min("Highest point"), max("Highest point"),
                         round(avg("Highest point"), 1) FROM raw.resorts
        UNION ALL SELECT 'Lowest point',  min("Lowest point"),  max("Lowest point"),
                         round(avg("Lowest point"), 1)  FROM raw.resorts
        UNION ALL SELECT 'Total slopes',  min("Total slopes"),  max("Total slopes"),
                         round(avg("Total slopes"), 1)  FROM raw.resorts
        UNION ALL SELECT 'Total lifts',   min("Total lifts"),   max("Total lifts"),
                         round(avg("Total lifts"), 1)   FROM raw.resorts
        UNION ALL SELECT 'Lift capacity', min("Lift capacity"), max("Lift capacity"),
                         round(avg("Lift capacity"), 1) FROM raw.resorts
        UNION ALL SELECT 'Latitude',      min("Latitude"),      max("Latitude"),
                         round(avg("Latitude"), 1)      FROM raw.resorts
        UNION ALL SELECT 'Longitude',     min("Longitude"),     max("Longitude"),
                         round(avg("Longitude"), 1)     FROM raw.resorts
    """).df().to_string(index=False))

    print("\n  Suspicious zeros (is 0 a real value, or a missing one?):")
    print(con.execute("""
        SELECT 'Price = 0'         AS check, count(*) AS n FROM raw.resorts WHERE "Price" = 0
        UNION ALL SELECT 'Snow cannons = 0',  count(*) FROM raw.resorts WHERE "Snow cannons" = 0
        UNION ALL SELECT 'Longest run = 0',   count(*) FROM raw.resorts WHERE "Longest run" = 0
        UNION ALL SELECT 'Total slopes = 0',  count(*) FROM raw.resorts WHERE "Total slopes" = 0
        UNION ALL SELECT 'Lift capacity = 0', count(*) FROM raw.resorts WHERE "Lift capacity" = 0
    """).df().to_string(index=False))

    _h("4. Internal consistency — do the parts sum to the totals?")
    print(con.execute("""
        SELECT
          count(*) FILTER (
            WHERE "Beginner slopes" + "Intermediate slopes" + "Difficult slopes"
                  <> "Total slopes")                                AS slopes_mismatch,
          count(*) FILTER (
            WHERE "Surface lifts" + "Chair lifts" + "Gondola lifts"
                  <> "Total lifts")                                 AS lifts_mismatch,
          count(*) FILTER (WHERE "Highest point" <= "Lowest point") AS elevation_inverted,
          count(*) FILTER (WHERE "ID" IS NULL)                      AS null_ids,
          count(*) - count(DISTINCT "ID")                           AS duplicate_ids
        FROM raw.resorts
    """).df().to_string(index=False))

    print("\n  Examples where slopes do not sum (if any):")
    ex = con.execute("""
        SELECT "ID", "Resort", "Beginner slopes" AS beg, "Intermediate slopes" AS int,
               "Difficult slopes" AS diff, "Total slopes" AS total,
               "Beginner slopes" + "Intermediate slopes" + "Difficult slopes" AS sum_parts
        FROM raw.resorts
        WHERE "Beginner slopes" + "Intermediate slopes" + "Difficult slopes" <> "Total slopes"
        LIMIT 8
    """).df()
    print("  none" if ex.empty else ex.to_string(index=False))

    _h("5. Text quality — encoding damage and categoricals")
    bad = con.execute("""
        SELECT "ID", "Resort", "Country"
        FROM raw.resorts
        WHERE "Resort" LIKE '%?%' OR "Country" LIKE '%?%'
        LIMIT 15
    """).df()
    print(f"  rows containing '?' (mojibake): {len(bad)}")
    if not bad.empty:
        print(bad.to_string(index=False))

    print("\n  Countries:")
    print(con.execute("""
        SELECT "Country", count(*) AS n FROM raw.resorts
        GROUP BY 1 ORDER BY n DESC LIMIT 12
    """).df().to_string(index=False))

    print("\n  Yes/No columns — distinct values (should be exactly Yes and No):")
    for c in ["Child friendly", "Snowparks", "Nightskiing", "Summer skiing"]:
        vals = con.execute(
            f'SELECT DISTINCT "{c}" FROM raw.resorts ORDER BY 1').df()[c].tolist()
        print(f"    {c:16} {vals}")

    print("\n  Season strings — the ones that wrap the year boundary are the hard case:")
    print(con.execute("""
        SELECT "Season", count(*) AS n FROM raw.resorts
        GROUP BY 1 ORDER BY n DESC LIMIT 12
    """).df().to_string(index=False))

    print("\n  Hemisphere sanity — southern resorts should have a Jun–Sep season:")
    print(con.execute("""
        SELECT CASE WHEN "Latitude" < 0 THEN 'southern' ELSE 'northern' END AS hemisphere,
               count(*) AS n_resorts,
               count(*) FILTER (WHERE "Season" LIKE '%June%'
                                   OR "Season" LIKE '%July%')  AS season_starts_jun_jul
        FROM raw.resorts GROUP BY 1
    """).df().to_string(index=False))


# ------------------------------------------------------------------------------- snow
def _profile_snow(con) -> None:
    _h("6. raw.snow — the global grid")
    print(con.execute("DESCRIBE raw.snow").df().to_string(index=False))
    print(con.execute("""
        SELECT count(*) AS n_rows,
               count(DISTINCT "Month")                       AS n_months,
               min("Month")                                  AS first_month,
               max("Month")                                  AS last_month,
               count(DISTINCT ("Latitude", "Longitude"))     AS n_cells,
               min("Snow") AS snow_lo, max("Snow") AS snow_hi
        FROM raw.snow
    """).df().to_string(index=False))

    _h("7. Grid spacing — verifying the 0.25° assumption")
    print(con.execute("""
        WITH lats AS (SELECT DISTINCT "Latitude" AS v FROM raw.snow ORDER BY 1 LIMIT 6)
        SELECT v AS latitude, round(v - lag(v) OVER (ORDER BY v), 4) AS step FROM lats
    """).df().to_string(index=False))

    print(f"\n  Assumed grid: {SNOW_GRID_DEG}° spacing, centres offset {SNOW_GRID_OFFSET}")
    off = con.execute(f"""
        SELECT count(*) AS cells_off_grid FROM (
          SELECT DISTINCT "Latitude" AS v FROM raw.snow
        ) WHERE abs((v - {SNOW_GRID_OFFSET}) / {SNOW_GRID_DEG}
                    - round((v - {SNOW_GRID_OFFSET}) / {SNOW_GRID_DEG})) > 1e-6
    """).df()
    print(off.to_string(index=False))

    _h("8. The spatial join — will every resort find a cell?")
    print(con.execute(f"""
        WITH snapped AS (
          SELECT r."ID",
                 floor(r."Latitude"  / {SNOW_GRID_DEG}) * {SNOW_GRID_DEG} + {SNOW_GRID_OFFSET} AS cell_lat,
                 floor(r."Longitude" / {SNOW_GRID_DEG}) * {SNOW_GRID_DEG} + {SNOW_GRID_OFFSET} AS cell_lon
          FROM raw.resorts r
        )
        SELECT count(*)                                        AS n_resorts,
               count(DISTINCT (cell_lat, cell_lon))            AS n_distinct_cells,
               count(*) FILTER (WHERE c.n IS NULL)             AS resorts_with_no_snow_data
        FROM snapped s
        LEFT JOIN (
          SELECT "Latitude" AS lat, "Longitude" AS lon, count(*) AS n
          FROM raw.snow GROUP BY 1, 2
        ) c ON c.lat = s.cell_lat AND c.lon = s.cell_lon
    """).df().to_string(index=False))
    print("\n  ^ if n_distinct_cells < n_resorts, two resorts share a cell — that is fine,")
    print("    but it means the join is many-to-one and the fan-out canary matters.")


if __name__ == "__main__":
    profile()
