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

# Outputs, absolute rather than relative to the working directory.
#
# These were `Path("models/metrics.json")` and friends until an orchestrator ran them —
# at which point they resolved against the scheduler's working directory and the pipeline
# wrote its artefacts somewhere nobody was looking. The Makefile hid the problem because
# `make` always runs from the repo root.
#
# Worth keeping in mind generally: "works when I run it" and "works when something else
# runs it" are different claims, and a relative path is the usual reason they diverge.
MODELS = ROOT / "models"
MODEL_PATH = MODELS / "pricing_model.joblib"
METRICS_PATH = MODELS / "metrics.json"

SITE = ROOT / "site"
SITE_DATA = SITE / "data.json"

DBT_DIR = ROOT / "dbt"

# NASA NEO snow grid: cell centres sit at .125 / .375 / .625 / .875, i.e. 0.25° spacing.
# Verified against the raw file rather than assumed.
SNOW_GRID_DEG = 0.25
SNOW_GRID_OFFSET = 0.125
