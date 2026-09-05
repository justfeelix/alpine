"""Paths and constants. One place, so nothing hardcodes a path twice."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
RAW = DATA / "raw" / "ski-resort-data"
CACHE = DATA / "cache"

RESORTS_CSV = RAW / "resorts.csv"
SNOW_CSV = RAW / "snow.csv"

WAREHOUSE = Path(os.environ.get("ALPINE_WAREHOUSE", DATA / "warehouse.duckdb"))

# NASA NEO snow grid: cell centres sit at .125 / .375 / .625 / .875, i.e. 0.25° spacing.
# Verified against the raw file rather than assumed.
SNOW_GRID_DEG = 0.25
SNOW_GRID_OFFSET = 0.125
