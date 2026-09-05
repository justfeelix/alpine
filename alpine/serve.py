"""Step 10 — the serving layer.

--------------------------------------------------------------------------------------
WHAT THIS FILE IS ALLOWED TO KNOW
--------------------------------------------------------------------------------------
**One dependency: `mart_resort_pricing`.** Not the CSVs, not the Open-Meteo cache, not the
staging or intermediate models. That is the entire payoff of the layered warehouse — the
cp1252 decoding, the 0.25° spatial join, the wrap-aware season predicate and the
coordinate cross-check are all upstream facts that this file never has to reason about.

The test of whether the layering was worth building is exactly this: how much of the
project's mess had to leak in here to make the API work? The answer should be none, and if
you read this file top to bottom you will not find a single mention of any of it.

--------------------------------------------------------------------------------------
THE THREE THINGS THIS SERVES
--------------------------------------------------------------------------------------
1. **The mart itself** — filtered, paginated, plus country-level aggregates. Reads.
2. **The evaluation** — `/model` returns the cross-validated metrics from metrics.json,
   *not* a score computed here. See the note in model.train_final: the served pipeline was
   refit on all rows and is deliberately never scored.
3. **Predictions** — for arbitrary input, and for the nine real resorts that have no price.
   That last endpoint is where a decision made back in the mart pays off: those rows were
   flagged and kept rather than dropped, precisely so something downstream could use them.

--------------------------------------------------------------------------------------
WHY THE CONNECTION AND THE MODEL ARE LOADED AT STARTUP
--------------------------------------------------------------------------------------
Both are expensive and both are immutable for the life of the process. Opening DuckDB per
request would dominate the response time; unpickling the model per request would be worse.
`lifespan` runs once on the way up, and — importantly — a failure there kills the process
loudly at boot instead of turning into a 500 on the first real request.

The Cadence lesson is baked into `/health`: it reports whether the model actually loaded,
because a health check that only pings the database will sit there returning 200 while
every endpoint that needs the model returns 500.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import METRICS_PATH, MODEL_PATH, SITE, WAREHOUSE
from .model import ALL_FEATURES, find_table

# Filled by the lifespan handler. Module-level rather than passed around because there is
# exactly one of each per process and FastAPI has no better place to put them.
STATE: dict[str, Any] = {"con": None, "table": None, "model": None,
                         "metrics": None, "errors": []}


# --------------------------------------------------------------------------- startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the warehouse and load the model once, recording failures rather than raising.

    Deliberately does NOT crash on a missing model. The read endpoints are useful on their
    own, and a service that refuses to start because one optional artefact is absent is
    harder to debug than one that starts and tells you precisely what is missing. `/health`
    reports the failures; the prediction endpoints return 503 with the same message.
    """
    import duckdb

    errors: list[str] = []

    try:
        STATE["con"] = duckdb.connect(str(WAREHOUSE), read_only=True)
        STATE["table"] = find_table(STATE["con"], "mart_resort_pricing")
    except Exception as e:                                    # noqa: BLE001
        errors.append(f"warehouse: {e}")

    try:
        import joblib
        STATE["model"] = joblib.load(MODEL_PATH)
    except FileNotFoundError:
        errors.append(f"model: {MODEL_PATH} not found — run `make model`")
    except Exception as e:                                    # noqa: BLE001
        # Most likely a scikit-learn version mismatch against whatever pickled it. The
        # artefact records the version it was written with, so say so.
        errors.append(f"model: failed to load ({type(e).__name__}: {e})")

    try:
        STATE["metrics"] = json.loads(METRICS_PATH.read_text())
    except Exception as e:                                    # noqa: BLE001
        errors.append(f"metrics: {e}")

    STATE["errors"] = errors
    yield

    if STATE["con"] is not None:
        STATE["con"].close()


app = FastAPI(
    title="Alpine",
    description="Ski-resort pricing and snow reliability, served from the dbt marts.",
    version="0.1.0",
    lifespan=lifespan,
)


# ----------------------------------------------------------------------- query helper
def query(sql: str, params: list | None = None) -> pd.DataFrame:
    """Run SQL against the warehouse on a per-request cursor.

    `con.cursor()` gives this request its own cursor over the same open database. FastAPI
    runs sync endpoints in a threadpool, so two requests genuinely can execute at the same
    time, and sharing one cursor across threads is how you get interleaved results.

    `params` is a list, and every user-supplied value goes through it. The only thing ever
    interpolated into the SQL string is the schema-qualified table name, which comes from
    the DuckDB catalog rather than from the request.
    """
    if STATE["con"] is None:
        raise HTTPException(503, detail="; ".join(STATE["errors"]) or "warehouse unavailable")
    cur = STATE["con"].cursor()
    try:
        return cur.execute(sql, params or []).df()
    finally:
        cur.close()


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records. NaN is not valid JSON; None is."""
    return df.astype(object).where(pd.notnull(df), None).to_dict("records")


def require_model() -> dict:
    if STATE["model"] is None:
        raise HTTPException(
            503, detail="; ".join(e for e in STATE["errors"] if e.startswith("model"))
                        or "model unavailable")
    return STATE["model"]


# --------------------------------------------------------------------------- schemas
class ResortFeatures(BaseModel):
    """The inputs to a prediction.

    **Every field is optional except country.** That is not laziness — the training
    pipeline begins with a median `SimpleImputer` fitted on the training data, so a missing
    numeric feature is filled with the same median the model saw during training. The API
    can therefore answer "what would a 120 km Austrian resort cost?" without being handed
    twenty-two columns. Country is required because it is one-hot encoded, carries by far
    the most signal, and imputing it would mean inventing the single most important fact.

    Unknown countries are safe: the encoder was fitted with `handle_unknown="ignore"`, so
    they encode to all-zeros and the prediction falls back to the global pattern.
    """
    country: str = Field(..., examples=["Austria"])

    vertical_drop_m: float | None = None
    highest_point_m: float | None = None
    lowest_point_m: float | None = None
    total_slopes_km: float | None = Field(None, examples=[120])
    pct_difficult_terrain: float | None = None
    pct_beginner_terrain: float | None = None

    total_lifts: float | None = None
    gondola_lifts: float | None = None
    lift_capacity_per_hour: float | None = None
    piste_km_per_lift: float | None = None
    capacity_per_piste_km: float | None = None
    snow_cannons: float | None = None
    season_length_months: float | None = None

    has_snowpark: bool | None = None
    has_night_skiing: bool | None = None
    has_summer_skiing: bool | None = None
    is_child_friendly: bool | None = None

    snow_cover_pct_in_season: float | None = None
    pct_season_months_above_50: float | None = None
    season_snowfall_cm: float | None = None
    pct_season_days_freezing: float | None = None
    season_avg_temp_max_c: float | None = None


class Prediction(BaseModel):
    predicted_price_eur: float
    expected_error_eur: float
    basis: str


def predict_frame(rows: list[dict]) -> list[float]:
    """Predict for a list of feature dicts.

    Builds the frame with `columns=ALL_FEATURES` so the column order is fixed by the
    training-time feature list rather than by whatever order the request happened to use.
    A ColumnTransformer selects by name, but the estimator behind it sees a positional
    array — reordering here would silently feed `total_lifts` into the `snow_cannons` slot
    and return a confident, wrong number.
    """
    model = require_model()
    X = pd.DataFrame(rows, columns=ALL_FEATURES)
    for c in ALL_FEATURES:
        if c != "country":
            X[c] = pd.to_numeric(X[c], errors="coerce")
    return [float(v) for v in model["pipeline"].predict(X)]


def expected_error() -> float:
    """The honest error bar: the cross-validated MAE, not a training-set number."""
    m = STATE.get("metrics") or {}
    best = min((r["mae"] for r in m.get("models", [])), default=float("nan"))
    return round(best, 2)


# -------------------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    """Reports what actually loaded, not merely that the process is alive.

    503 when anything is missing, so a container orchestrator or a verify script sees the
    failure instead of a cheerful 200 from a service that cannot answer a single question.
    """
    ready = STATE["con"] is not None and STATE["model"] is not None
    body = {
        "status": "ok" if ready else "degraded",
        "warehouse": str(WAREHOUSE),
        "table": STATE["table"],
        "model_loaded": STATE["model"] is not None,
        "model_sklearn_version": (STATE["model"] or {}).get("sklearn_version"),
        "metrics_loaded": STATE["metrics"] is not None,
        "errors": STATE["errors"],
    }
    if not ready:
        raise HTTPException(503, detail=body)
    return body


@app.get("/resorts")
def list_resorts(
    country: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_snow: float | None = Query(None, ge=0, le=100),
    model_ready_only: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """The mart, filtered and paginated.

    Filters are assembled as a list of predicates and joined with AND — each one appends
    both its SQL fragment and its parameter, so the two lists cannot drift out of step.
    The alternative, string-concatenating the values, is the SQL-injection bug.

    `limit` is capped at 500 by `Query(le=500)`: an unbounded list endpoint is a way for
    one request to pull the whole table into memory.
    """
    where, params = [], []
    if country is not None:
        where.append("lower(country) = lower(?)")
        params.append(country)
    if min_price is not None:
        where.append("price_eur >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("price_eur <= ?")
        params.append(max_price)
    if min_snow is not None:
        where.append("snow_cover_pct_in_season >= ?")
        params.append(min_snow)
    if model_ready_only:
        where.append("is_model_ready")

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = query(f"SELECT count(*) AS n FROM {STATE['table']} {clause}",
                  params).n.iloc[0]
    df = query(
        f"SELECT * FROM {STATE['table']} {clause} "
        f"ORDER BY price_eur DESC NULLS LAST, resort_name LIMIT ? OFFSET ?",
        params + [limit, offset])

    return {"total": int(total), "limit": limit, "offset": offset,
            "resorts": records(df)}


@app.get("/resorts/{resort_id}")
def get_resort(resort_id: int):
    """One resort, with the model's opinion of what it should cost.

    The residual (actual − predicted) is the interesting column: it is the part of the
    price the mountain does not explain. A large positive residual is a resort charging
    more than its terrain, country and snow would suggest — which, in a pricing context,
    is the row a human would want to look at.
    """
    df = query(f"SELECT * FROM {STATE['table']} WHERE resort_id = ?", [resort_id])
    if df.empty:
        raise HTTPException(404, detail=f"resort_id {resort_id} not found")

    row = records(df)[0]
    if STATE["model"] is not None:
        pred = predict_frame([{c: row.get(c) for c in ALL_FEATURES}])[0]
        row["predicted_price_eur"] = round(pred, 2)
        row["residual_eur"] = (round(row["price_eur"] - pred, 2)
                               if row.get("price_eur") is not None else None)
    return row


@app.get("/countries")
def countries():
    """Country-level aggregates — the shape of the headline finding, as data.

    Serving this is not decoration. Country is the feature that mattered four times more
    than anything else, and the reason snow *looked* like it mattered was that the snowy
    countries are the expensive ones. This endpoint returns both columns side by side, so
    a chart can show that confound rather than a paragraph having to assert it.
    """
    df = query(f"""
        SELECT country,
               count(*)                                   AS n_resorts,
               count(*) FILTER (WHERE is_model_ready)     AS n_model_ready,
               round(avg(price_eur), 1)                   AS avg_price_eur,
               round(median(price_eur), 1)                AS median_price_eur,
               round(avg(snow_cover_pct_in_season), 1)    AS avg_snow_cover_pct,
               round(avg(total_slopes_km), 1)             AS avg_slopes_km
        FROM {STATE['table']}
        GROUP BY country
        HAVING count(*) >= 5
        ORDER BY avg_price_eur DESC
    """)
    return {"countries": records(df)}


@app.get("/model")
def model_card():
    """The evaluation, served verbatim from metrics.json.

    `lift_over_baseline` is computed here and put first on purpose. An R² alone invites the
    reader to be impressed by 0.73; stated against the country median, the honest claim is
    "23% better than the obvious guess", and that is the number to defend.
    """
    m = STATE["metrics"]
    if m is None:
        raise HTTPException(503, detail="metrics.json not found — run `make model`")

    best_base = min(m["baselines"], key=lambda r: r["mae"])
    best_model = min(m["models"], key=lambda r: r["mae"])
    abl = {r["name"]: r for r in m["snow_ablation"]}
    without = abl["ridge WITHOUT snow features"]

    return {
        "headline": {
            "best_baseline": best_base["name"],
            "best_baseline_mae_eur": round(best_base["mae"], 2),
            "best_model": best_model["name"],
            "best_model_mae_eur": round(best_model["mae"], 2),
            "lift_over_baseline_eur": round(best_base["mae"] - best_model["mae"], 2),
            "lift_over_baseline_pct": round(
                100 * (best_base["mae"] - best_model["mae"]) / best_base["mae"], 1),
        },
        "snow_verdict": {
            "mae_delta_eur": round(m["snow_ablation_mae_delta"], 2),
            "fold_spread_eur": round(without["mae_std"], 2),
            # The comparison that decides it, computed rather than asserted: a difference
            # smaller than the fold-to-fold spread is not a finding.
            "significant": abs(m["snow_ablation_mae_delta"]) > without["mae_std"],
            "conclusion": "no measurable effect once country and size are controlled for",
        },
        "n_training_rows": (STATE["model"] or {}).get("n_training_rows"),
        "evaluation": "5-fold cross-validation; the served model is refit on all rows",
        "baselines": m["baselines"],
        "models": m["models"],
        "snow_ablation": m["snow_ablation"],
        "top_features": m["top_features"],
    }


@app.post("/predict", response_model=Prediction)
def predict(features: ResortFeatures):
    """Price a hypothetical resort.

    The response carries `expected_error_eur` alongside the number, because a bare point
    estimate invites false precision. EUR 44.10 ± 6.62 is the truthful version of what this
    model knows, and the ± is the cross-validated MAE — measured on resorts the model had
    never seen.
    """
    value = predict_frame([features.model_dump()])[0]
    return Prediction(
        predicted_price_eur=round(value, 2),
        expected_error_eur=expected_error(),
        basis="gradient boosting, 5-fold CV MAE",
    )


@app.get("/predictions/missing-price")
def missing_price():
    """Predict for the resorts that have no price in the source.

    This is the endpoint that justifies a decision made three layers upstream. The mart
    could have dropped these nine rows — they cannot train the model, after all. It kept
    them flagged instead, on the grounds that a row unusable as a *training example* is
    still perfectly usable as a *prediction target*: their terrain and lift data is intact.

    Filtering on `exclusion_reason = 'no_price'` rather than `price_eur IS NULL` is the
    point of that column: it distinguishes "we have no price" from the other five reasons a
    resort might be excluded, and only this one is predictable.
    """
    df = query(f"SELECT * FROM {STATE['table']} WHERE exclusion_reason = 'no_price'")
    if df.empty:
        return {"n": 0, "predictions": []}

    rows = records(df)
    preds = predict_frame([{c: r.get(c) for c in ALL_FEATURES} for r in rows])

    return {
        "n": len(rows),
        "expected_error_eur": expected_error(),
        "note": "Predicted, not observed. These rows are absent from the training set.",
        "predictions": [
            {"resort_id": r["resort_id"], "resort_name": r["resort_name"],
             "country": r["country"], "total_slopes_km": r["total_slopes_km"],
             "snow_cover_pct_in_season": r["snow_cover_pct_in_season"],
             # Several of these nine also have suspect coordinates, so their snow columns
             # were NULLed upstream and the imputer supplied a median. The prediction is
             # still reasonable — snow turned out not to matter — but a consumer should be
             # told which numbers rest on invented inputs rather than measured ones.
             "snow_data_imputed": r["snow_cover_pct_in_season"] is None,
             "name_encoding_damaged": bool(r.get("has_encoding_damage")),
             "predicted_price_eur": round(p, 2)}
            for r, p in zip(rows, preds)
        ],
    }


# ----------------------------------------------------------------------- the frontend
# Mounted LAST, and that ordering is load-bearing. FastAPI matches routes in registration
# order, so a StaticFiles mount at "/" declared earlier would swallow /health, /resorts and
# everything else. Every API route above is registered first and therefore wins; the mount
# only sees what nothing else claimed.
#
# `html=True` serves index.html for "/". The page then probes the relative URL `health`:
# here that resolves to /health and answers, so the prediction panel unlocks. Published to
# GitHub Pages it resolves to /<repo>/health and 404s, so the same file falls back to the
# exported data.json. One page, no build step, no second frontend to keep in sync.
#
# `check_dir=False` because site/ does not exist until `make publish` has run, and the API
# is perfectly useful before then — refusing to boot over a missing frontend would be a
# worse failure than serving 404s for the page.
app.mount("/", StaticFiles(directory=SITE, html=True, check_dir=False), name="site")
