"""Step 2 — load the Kaggle CSVs into the warehouse, raw.

This is the **L** of ELT and nothing else. The data lands as the source gave it: original
column names, original values, original mistakes. Every interpretation happens later, in
dbt, in SQL, under version control.

Why that separation matters: if we cleaned on the way in and got it wrong, the original
would be gone. Loading raw means a definition can change and history can be rebuilt.

--------------------------------------------------------------------------------------
ENCODING — the first real problem in this dataset
--------------------------------------------------------------------------------------
`resorts.csv` is **not UTF-8**. DuckDB refuses it outright:

    Invalid unicode (byte sequence mismatch) detected. This file is not utf-8 encoded.

The file is **cp1252** (Windows-1252) — an Excel export from a Windows machine. Byte 0x96
is an en-dash there; in latin-1 the same byte is an undefined control character, so
"latin-1 nearly works" is a trap: it decodes without error and silently mangles 23 resort
names.

Note that decoding is *not* transformation. Choosing the right character encoding is part
of reading the bytes correctly — getting it wrong doesn't give you different data, it gives
you corrupt data. So it belongs here, in extract, not in dbt.

Two kinds of damage are present, and only one is fixable:

  * **Recoverable** — `Espace San Bernardo \x96 La Rosière`. Correct bytes, wrong decoder.
    Decoding as cp1252 restores the en-dash and the accent.
  * **Unrecoverable** — `La Rosière/?La Thuile`, `Nevados de Chilla?n`. The `?` was written
    into the file by whatever produced it; the original character is already gone. We flag
    these in staging rather than pretending to fix them.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from .config import RESORTS_CSV, SNOW_CSV, WAREHOUSE

# Determined empirically, not guessed: cp1252 is the only candidate that decodes 0x96 to an
# en-dash rather than a control character. See the module docstring.
RESORTS_ENCODING = "cp1252"


def connect() -> duckdb.DuckDBPyConnection:
    WAREHOUSE.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(WAREHOUSE))


def load_seed(con: duckdb.DuckDBPyConnection | None = None) -> dict[str, int]:
    """Read both CSVs into schema `raw`. Idempotent. Returns row counts."""
    owned = con is None
    con = con or connect()
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")

        # pandas rather than DuckDB's reader, purely because DuckDB's CSV encoding support
        # doesn't cover cp1252. Column names and values are otherwise untouched.
        resorts = pd.read_csv(RESORTS_CSV, encoding=RESORTS_ENCODING)
        con.register("_resorts", resorts)
        con.execute("CREATE OR REPLACE TABLE raw.resorts AS SELECT * FROM _resorts")
        con.unregister("_resorts")

        counts = {
            "raw.resorts": con.execute("SELECT count(*) FROM raw.resorts").fetchone()[0]
        }

        if SNOW_CSV.exists():
            # snow.csv is 820k rows of pure ASCII — DuckDB reads it directly and fast.
            con.execute(f"""
                CREATE OR REPLACE TABLE raw.snow AS
                SELECT * FROM read_csv_auto('{SNOW_CSV}', header=true)
            """)
            counts["raw.snow"] = con.execute(
                "SELECT count(*) FROM raw.snow").fetchone()[0]

        return counts
    finally:
        if owned:
            con.close()


if __name__ == "__main__":
    for table, n in load_seed().items():
        print(f"{table:16} {n:>10,} rows")
