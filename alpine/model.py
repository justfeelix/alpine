"""Step 9 — the price model, and the question it exists to answer.

    Does snow reliability command a price premium, once you control for the
    obvious things — size, altitude, lifts, and which country you're in?

--------------------------------------------------------------------------------------
HOW THIS IS ORGANISED, AND WHY
--------------------------------------------------------------------------------------
**Baselines before models.** Three of them, in increasing difficulty: predict the global
mean, predict the country median, predict from terrain alone. A model that cannot beat
"the median price in this country" has told you nothing, and reporting R² without that
comparison is how a useless model gets shipped.

**Cross-validation, not a single split.** 422 rows. An 80/20 split puts 84 resorts in the
test set, and the score would move several euro depending on the seed. 5-fold CV with the
spread reported is the honest version — and the spread matters as much as the mean.

**No time dimension, so no time-based split.** This is cross-sectional: one row per resort,
all measured in the same period. The leakage risk here is not the future, it is
`price_per_piste_km` — a column derived from the target. It is excluded, deliberately and
by name.

**The question is answered by ablation, not by a coefficient.** Fit the model with and
without the snow features and compare. A coefficient tells you about one model's internal
arithmetic; the ablation tells you whether the information helps at all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import KFold, cross_val_predict, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .config import WAREHOUSE

TARGET = "price_eur"
SEED = 42
N_FOLDS = 5

# ---------------------------------------------------------------------------- features
# The mountain and the operation. Nothing here is derived from price.
TERRAIN_FEATURES = [
    "vertical_drop_m", "highest_point_m", "lowest_point_m",
    "total_slopes_km", "pct_difficult_terrain", "pct_beginner_terrain",
]

OPERATION_FEATURES = [
    "total_lifts", "gondola_lifts", "lift_capacity_per_hour",
    "piste_km_per_lift", "capacity_per_piste_km", "snow_cannons",
    "season_length_months",
]

BOOLEAN_FEATURES = [
    "has_snowpark", "has_night_skiing", "has_summer_skiing", "is_child_friendly",
]

# The features under investigation. Everything above is the control group.
SNOW_FEATURES = [
    "snow_cover_pct_in_season", "pct_season_months_above_50",
    "season_snowfall_cm", "pct_season_days_freezing", "season_avg_temp_max_c",
]

CATEGORICAL_FEATURES = ["country"]

# EXCLUDED, and worth being explicit about:
#   price_per_piste_km  - computed FROM the target. Including it would produce a superb
#                         score and a worthless model. This is the leakage risk in a
#                         cross-sectional problem: not the future, but a derived column.
#   longest_run_km      - 164 of 422 rows are NULL (the source encodes missing as 0, which
#                         staging turned into NULL). Too sparse to be worth imputing.
#   resort_name         - an identifier, not a feature.
LEAKY_OR_UNUSABLE = ["price_per_piste_km", "longest_run_km", "resort_name", "resort_id"]


@dataclass
class Result:
    name: str
    mae: float
    mae_std: float
    r2: float
    r2_std: float
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def row(self) -> str:
        return (f"  {self.name:<34} {self.mae:>6.2f} ± {self.mae_std:<5.2f} "
                f"{self.r2:>6.3f} ± {self.r2_std:<5.3f}  {self.notes}")


# ------------------------------------------------------------------------------ data
def find_table(con, table: str) -> str:
    """Locate a dbt-built table, whatever schema it landed in.

    dbt prefixes a custom schema with the target schema, so `+schema: marts` against the
    default DuckDB target produces `main_marts`, not `marts`. Hardcoding either name breaks
    the moment the target changes — and it will, because the Snowflake profile uses a
    different one. Asking the catalog is both shorter and correct everywhere.
    """
    rows = con.execute(
        "SELECT schema_name FROM duckdb_tables() WHERE table_name = ?"
        " UNION SELECT schema_name FROM duckdb_views() WHERE view_name = ?",
        [table, table]).fetchall()
    if not rows:
        raise RuntimeError(
            f"'{table}' not found in {WAREHOUSE}. Run `cd dbt && dbt build` first.")
    return f"{rows[0][0]}.{table}"


def load(con=None) -> pd.DataFrame:
    """The modelling table, already filtered by the mart's own eligibility rule."""
    import duckdb

    owned = con is None
    con = con or duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        table = find_table(con, "mart_resort_pricing")
        return con.execute(f"SELECT * FROM {table} WHERE is_model_ready").df()
    finally:
        if owned:
            con.close()


def build_pipeline(numeric: list[str], categorical: list[str], estimator) -> Pipeline:
    """Preprocessing inside the pipeline, so it is fit on training folds only.

    Scaling or imputing before the split leaks test-set statistics into training. Inside a
    Pipeline, `fit` sees one fold at a time and `predict` reuses those fitted parameters —
    which is the whole reason this is a Pipeline rather than three lines of pandas.
    """
    return Pipeline([
        ("prep", ColumnTransformer([
            ("num", Pipeline([
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
            ]), numeric),
            ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=5), categorical),
        ])),
        ("model", estimator),
    ])


def evaluate(pipe, X, y, name: str, notes: str = "") -> Result:
    """5-fold CV. Reports the spread, because the spread is the point."""
    cv = KFold(n_splits=N_FOLDS, shuffle=True, random_state=SEED)
    mae = -cross_val_score(pipe, X, y, cv=cv, scoring="neg_mean_absolute_error")
    r2 = cross_val_score(pipe, X, y, cv=cv, scoring="r2")
    return Result(name, mae.mean(), mae.std(), r2.mean(), r2.std(), notes)


# ------------------------------------------------------------------------- baselines
def baselines(df: pd.DataFrame) -> list[Result]:
    """What the model has to beat. Computed first, on purpose."""
    y = df[TARGET]
    results = []

    # 1. The global mean. The floor: any model below this is actively harmful.
    results.append(evaluate(
        Pipeline([("m", DummyRegressor(strategy="mean"))]),
        df[["vertical_drop_m"]], y,
        "baseline: global mean", "the floor"))

    # 2. The country median. A genuinely hard baseline — most of the variance in lift-pass
    #    price is "which country is this", and any human would guess this way.
    cv = KFold(n_splits=N_FOLDS, shuffle=True, random_state=SEED)
    maes, r2s = [], []
    for train_idx, test_idx in cv.split(df):
        tr, te = df.iloc[train_idx], df.iloc[test_idx]
        medians = tr.groupby("country")[TARGET].median()
        pred = te["country"].map(medians).fillna(tr[TARGET].median())
        maes.append(mean_absolute_error(te[TARGET], pred))
        r2s.append(r2_score(te[TARGET], pred))
    results.append(Result("baseline: country median",
                          float(np.mean(maes)), float(np.std(maes)),
                          float(np.mean(r2s)), float(np.std(r2s)),
                          "the one that matters"))

    # 3. Terrain only, no snow, no country. Isolates how far physical size alone gets you.
    results.append(evaluate(
        build_pipeline(TERRAIN_FEATURES, [], Ridge(alpha=1.0)),
        df, y, "baseline: terrain only (ridge)", "size alone"))

    return results


# ---------------------------------------------------------------------------- models
def fit_models(df: pd.DataFrame) -> list[Result]:
    y = df[TARGET]
    numeric = TERRAIN_FEATURES + OPERATION_FEATURES + SNOW_FEATURES + BOOLEAN_FEATURES
    results = []

    results.append(evaluate(
        build_pipeline(numeric, CATEGORICAL_FEATURES, Ridge(alpha=1.0)),
        df, y, "ridge: all features", ""))

    results.append(evaluate(
        build_pipeline(numeric, CATEGORICAL_FEATURES,
                       HistGradientBoostingRegressor(
                           max_iter=300, learning_rate=0.06, max_depth=4,
                           l2_regularization=1.0, random_state=SEED)),
        df, y, "gradient boosting: all features", ""))

    return results


def snow_ablation(df: pd.DataFrame) -> list[Result]:
    """The actual question: does snow information add anything the rest does not?

    Two identical pipelines, one with the snow features and one without. If the snow
    version is not better, then whatever relationship exists between snow and price is
    already explained by altitude, size and country — which would itself be a real finding.
    """
    y = df[TARGET]
    without = TERRAIN_FEATURES + OPERATION_FEATURES + BOOLEAN_FEATURES
    with_snow = without + SNOW_FEATURES

    return [
        evaluate(build_pipeline(without, CATEGORICAL_FEATURES, Ridge(alpha=1.0)),
                 df, y, "ridge WITHOUT snow features", "control"),
        evaluate(build_pipeline(with_snow, CATEGORICAL_FEATURES, Ridge(alpha=1.0)),
                 df, y, "ridge WITH snow features", "treatment"),
    ]


def feature_importance(df: pd.DataFrame, n_repeats: int = 10) -> pd.DataFrame:
    """Permutation importance on held-out predictions, against the metric we report."""
    y = df[TARGET]
    numeric = TERRAIN_FEATURES + OPERATION_FEATURES + SNOW_FEATURES + BOOLEAN_FEATURES
    pipe = build_pipeline(numeric, CATEGORICAL_FEATURES,
                          HistGradientBoostingRegressor(
                              max_iter=300, learning_rate=0.06, max_depth=4,
                              l2_regularization=1.0, random_state=SEED))

    cols = numeric + CATEGORICAL_FEATURES

    # Pass ONLY the feature columns. permutation_importance shuffles every column of the
    # frame it is given, so handing it the whole mart would produce importances for
    # resort_name and exclusion_reason alongside the real features — and a length mismatch
    # when zipping the results back to names.
    X = df[cols]

    cv = KFold(n_splits=N_FOLDS, shuffle=True, random_state=SEED)
    train_idx, test_idx = next(iter(cv.split(X)))
    pipe.fit(X.iloc[train_idx], y.iloc[train_idx])

    imp = permutation_importance(
        pipe, X.iloc[test_idx], y.iloc[test_idx],
        n_repeats=n_repeats, random_state=SEED, scoring="neg_mean_absolute_error")
    return (pd.DataFrame({"feature": cols,
                          "importance_eur": imp.importances_mean,
                          "std": imp.importances_std})
            .sort_values("importance_eur", ascending=False)
            .reset_index(drop=True))


# ------------------------------------------------------------------------------- run
def run(save_to: str | None = None) -> dict:
    df = load()
    print(f"Model-ready resorts: {len(df)}  "
          f"(target: EUR {df[TARGET].min():.0f}-{df[TARGET].max():.0f}, "
          f"mean {df[TARGET].mean():.1f})\n")

    print("=" * 78)
    print("  BASELINES FIRST — a score with nothing to beat is not a result")
    print("=" * 78)
    print(f"  {'':<34} {'MAE (EUR)':>13} {'R2':>15}")
    base = baselines(df)
    for r in base:
        print(r.row())

    print("\n" + "=" * 78)
    print("  MODELS")
    print("=" * 78)
    models = fit_models(df)
    for r in models:
        print(r.row())

    best_base = min(base, key=lambda r: r.mae)
    best_model = min(models, key=lambda r: r.mae)
    improvement = 100 * (best_base.mae - best_model.mae) / best_base.mae
    print(f"\n  Best model beats the best baseline by "
          f"EUR {best_base.mae - best_model.mae:.2f} MAE ({improvement:.1f}%)")

    print("\n" + "=" * 78)
    print("  THE QUESTION: does snow reliability add anything?")
    print("=" * 78)
    abl = snow_ablation(df)
    for r in abl:
        print(r.row())
    delta = abl[0].mae - abl[1].mae
    print(f"\n  Adding snow features changes MAE by EUR {delta:+.2f} "
          f"({100 * delta / abl[0].mae:+.1f}%)")
    print(f"  Fold-to-fold spread is about EUR {abl[0].mae_std:.2f}, so a difference "
          f"smaller than that is noise.")

    print("\n" + "=" * 78)
    print("  PERMUTATION IMPORTANCE (EUR of MAE lost when shuffled)")
    print("=" * 78)
    imp = feature_importance(df)
    print(imp.head(12).to_string(index=False))

    summary = {
        "n_resorts": int(len(df)),
        "baselines": [r.__dict__ for r in base],
        "models": [r.__dict__ for r in models],
        "snow_ablation": [r.__dict__ for r in abl],
        "snow_ablation_mae_delta": float(delta),
        "top_features": imp.head(10).to_dict("records"),
    }
    if save_to:
        with open(save_to, "w") as f:
            json.dump(summary, f, indent=2, default=float)
        print(f"\nSaved -> {save_to}")
    return summary


if __name__ == "__main__":
    run()
