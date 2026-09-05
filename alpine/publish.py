"""Step 11 — export the marts to a static JSON bundle for the frontend.

--------------------------------------------------------------------------------------
WHY THIS STEP EXISTS AT ALL
--------------------------------------------------------------------------------------
GitHub Pages serves static files. It will not run Python, so the FastAPI service cannot
live there — which leaves two bad options and one good one:

    (a) host the API somewhere with a server   → real infrastructure, real cost, and the
                                                 warehouse is a 12 MB local file
    (b) build a second, cut-down frontend      → two pages that drift apart within a week
    (c) freeze the answers to a JSON file      → one page, two data sources

(c) is what this does. The page fetches `data.json` and renders everything from it. If it
*also* finds a live API at the same origin, it enables the interactive prediction form on
top. One HTML file, no build step, works in both places.

**This is a publishing step, not a caching hack.** Every number on the page is derived from
the warehouse at export time and stamped with when. The honest framing is that the site is
a *snapshot* — and it says so in the footer, because a dashboard that cannot tell you how
stale it is will eventually be trusted when it shouldn't be.

--------------------------------------------------------------------------------------
WHAT GETS EXPORTED, AND WHAT DELIBERATELY DOES NOT
--------------------------------------------------------------------------------------
Exported: the 499 resorts (a projection, not `SELECT *`), country aggregates, the model
card, and a prediction for every resort. About 200 KB — small enough that the page loads
in one request and filters client-side, which is why there is no pagination in the UI.

Not exported: the 182,135 daily weather rows, and the raw snow raster. They are the
*inputs* to the numbers here. Shipping them would multiply the payload by two orders of
magnitude to let a browser recompute something dbt already computed correctly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import METRICS_PATH, MODEL_PATH, SITE_DATA, WAREHOUSE
from .model import ALL_FEATURES, find_table

# A projection, not SELECT *. The mart has 38 columns; the page uses these. Naming them
# means adding a column upstream cannot silently inflate the payload every visitor
# downloads — and it documents, in one place, exactly what the frontend depends on.
COLUMNS = [
    "resort_id", "resort_name", "country", "continent",
    "latitude", "longitude",
    "price_eur",
    "total_slopes_km", "vertical_drop_m", "highest_point_m", "lowest_point_m",
    "pct_difficult_terrain", "pct_beginner_terrain",
    "total_lifts", "gondola_lifts", "lift_capacity_per_hour", "snow_cannons",
    "season_length_months",
    "snow_cover_pct_in_season", "season_snowfall_cm", "pct_season_days_freezing",
    "season_avg_temp_max_c",
    "is_model_ready", "coordinates_suspect", "has_encoding_damage", "exclusion_reason",
]


def clean(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records. NaN is not valid JSON; null is."""
    return json.loads(df.to_json(orient="records"))


def build(warehouse: Path = WAREHOUSE) -> dict:
    import duckdb

    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        table = find_table(con, "mart_resort_pricing")
        df = con.execute(f"SELECT {', '.join(COLUMNS)} FROM {table}").df()

        countries = con.execute(f"""
            SELECT country,
                   count(*)                                AS n_resorts,
                   count(*) FILTER (WHERE is_model_ready)  AS n_model_ready,
                   round(avg(price_eur), 1)                AS avg_price_eur,
                   round(median(price_eur), 1)             AS median_price_eur,
                   round(avg(snow_cover_pct_in_season), 1) AS avg_snow_cover_pct,
                   round(avg(total_slopes_km), 1)          AS avg_slopes_km
            FROM {table}
            GROUP BY country
            HAVING count(*) >= 5
            ORDER BY avg_price_eur DESC
        """).df()
    finally:
        con.close()

    # A prediction for every resort, including the ones that trained the model.
    #
    # For a *trained-on* resort this is partly memory, not forecast — which is exactly why
    # the page labels residuals as in-sample. The number is still worth showing: the
    # residual is the part of the price the mountain does not explain, and that is the
    # column a pricing analyst would actually want to sort by.
    try:
        import joblib
        model = joblib.load(MODEL_PATH)
        X = df.reindex(columns=ALL_FEATURES)
        for c in ALL_FEATURES:
            if c != "country":
                X[c] = pd.to_numeric(X[c], errors="coerce")
        df["predicted_price_eur"] = [round(float(v), 2)
                                     for v in model["pipeline"].predict(X)]
        df["residual_eur"] = (df["price_eur"] - df["predicted_price_eur"]).round(2)
        model_note = (f"gradient boosting, refit on {model['n_training_rows']} "
                      f"model-ready resorts")
    except Exception as e:                                    # noqa: BLE001
        # Export must not fail because the model is missing — the descriptive half of the
        # page is useful on its own, and the frontend hides the prediction columns when
        # they are absent rather than rendering empty cells.
        print(f"  ! no predictions: {type(e).__name__}: {e}")
        model_note = "unavailable"

    metrics = json.loads(METRICS_PATH.read_text()) if METRICS_PATH.exists() else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Kaggle ski-resort dataset + NASA NEO snow cover + Open-Meteo ERA5",
        "model_note": model_note,
        "counts": {
            "resorts": int(len(df)),
            "model_ready": int(df.is_model_ready.sum()),
            "suspect_coordinates": int(df.coordinates_suspect.sum()),
            "encoding_damaged": int(df.has_encoding_damage.fillna(False).sum()),
            "no_price": int((df.exclusion_reason == "no_price").sum()),
        },
        "resorts": clean(df),
        "countries": clean(countries),
        "metrics": metrics,
    }


def publish(out: Path = SITE_DATA) -> Path:
    bundle = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, separators=(",", ":")))
    kb = out.stat().st_size / 1024
    print(f"  {bundle['counts']['resorts']} resorts, "
          f"{len(bundle['countries'])} countries -> {out} ({kb:.0f} KB)")
    return out
