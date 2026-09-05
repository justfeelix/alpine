"""Step 10.5 — the Alpine pipeline as an Airflow DAG.

======================================================================================
THE ONE RULE: THE DAG CONTAINS NO BUSINESS LOGIC
======================================================================================
Every task below is three lines that call a function defined elsewhere. There is no SQL
here, no pandas, no feature engineering, no API handling. That is deliberate and it is
the single most important thing about this file.

An orchestrator's job is to decide **what runs, when, in what order, and what happens
when it fails**. The moment transformation logic moves into a DAG it becomes untestable
(you need a scheduler to run it), unportable (it only works under Airflow) and invisible
to dbt's lineage. Everything here is still runnable as `python -m alpine.cli pipeline`
with Airflow uninstalled — and that is the test of whether the boundary held.

======================================================================================
WHAT AIRFLOW ADDS OVER THE MAKEFILE
======================================================================================
The Makefile already encodes this dependency graph, and for a laptop run it is enough.
Airflow adds four things a Makefile structurally cannot:

    retries      `weather` talks to a rate-limited public API. Airflow retries it with
                 backoff as infrastructure, rather than every caller reinventing it.
    scheduling   a cron expression and a scheduler process that honours it.
    resumption   a failed run restarts from the failed task, not from `seed`.
    observability per-task logs and duration history, weeks later, in a UI.

======================================================================================
HONEST ASSESSMENT
======================================================================================
For *this* pipeline Airflow is over-engineered, and it is worth saying so rather than
pretending otherwise. The Kaggle CSV never changes and the weather is a frozen 2022
snapshot, so `schedule=None` below is not laziness — it is the truthful setting. There
is nothing to schedule.

Airflow earns its keep the moment data arrives incrementally: fetch yesterday's weather
each morning, rebuild the marts, retrain weekly, republish. That version is sketched at
the bottom of this file, and it is the one worth describing.

Adding it did surface a real bug, though, which is the usual argument for orchestration:
every output path was relative, so the pipeline only worked when the working directory
happened to be the repo root. `make` always runs from there; a scheduler does not. Those
paths are now absolute in `alpine/config.py`.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.sdk import dag, task
from airflow.providers.standard.operators.bash import BashOperator

# The DAG file lives inside the repo, so the repo root is two levels up. Adding it to
# sys.path is what lets these tasks import the same `alpine` package the CLI uses —
# rather than a copy vendored into the DAGs folder, which would drift immediately.
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Airflow gets its own virtualenv, because it pins a large dependency tree and merging it
# with the project's is a fight worth avoiding. The consequence is that `dbt` is not on
# the PATH Airflow sees, so it has to be resolved explicitly.
#
# This is the ordinary shape of the problem in production too — the orchestrator and the
# thing it orchestrates rarely share an environment. Airflow's heavier answers are
# ExternalPythonOperator, DockerOperator and KubernetesPodOperator; naming the binary is
# the honest small-scale version.
def _find_dbt() -> str:
    """Locate the dbt binary: explicit override, then a nearby venv, then PATH.

    Searches REPO.parent as well as REPO, because a virtualenv shared across several
    projects usually sits *beside* the repo rather than inside it — which is exactly how
    this one is laid out, and exactly what the first version of this function got wrong.
    """
    if override := os.environ.get("ALPINE_DBT_BIN"):
        return override
    for base in (REPO, REPO.parent):
        for venv in (".venv-1", ".venv", ".venv-alpine", "venv", "env"):
            candidate = base / venv / "bin" / "dbt"
            if candidate.exists():
                return str(candidate)
    # Last resort. If dbt is genuinely absent the BashOperator fails with "command not
    # found", so make the DAG say what to do about it rather than leaving that to guesswork.
    return shutil.which("dbt") or (
        "echo 'dbt not found — set ALPINE_DBT_BIN to its path' >&2; exit 127; #")


DBT_BIN = _find_dbt()


@dag(
    dag_id="alpine_pipeline",
    description="Ski-resort pricing: extract -> dbt -> model -> publish",

    # None = manual trigger only, which is the honest setting for static source data.
    # The incremental version would use "0 6 * * *".
    schedule=None,

    start_date=datetime(2026, 1, 1),

    # Without this, enabling a DAG with a past start_date makes Airflow immediately
    # queue one run per missed interval — hundreds of runs, all hammering Open-Meteo.
    # It is the single most common way a first Airflow DAG surprises its author.
    catchup=False,

    # One run at a time. Every task writes to the same DuckDB file, and DuckDB takes an
    # exclusive lock on write — two concurrent runs would deadlock rather than race,
    # which is at least a loud failure, but it is better not to find out.
    max_active_runs=1,

    default_args={
        "owner": "felix",
        "retries": 1,
        "retry_delay": timedelta(minutes=1),
    },
    tags=["alpine", "elt", "dbt", "duckdb"],
)
def alpine_pipeline():

    # ---------------------------------------------------------------------- extract
    @task
    def seed() -> dict[str, int]:
        """Kaggle CSVs -> raw. Idempotent: truncates and reloads.

        Idempotency is what makes a retry safe. If this appended instead of replacing,
        Airflow's own retry would silently double the data — the orchestrator would turn
        a transient failure into a corrupt warehouse.
        """
        from alpine.seed import load_seed
        return load_seed()

    @task(
        # The only task that touches a rate-limited public API, so it gets its own retry
        # policy rather than inheriting the DAG default. Five minutes, not one: a 429
        # means a *quota* is exhausted, and retrying quickly just burns more of it.
        retries=3,
        retry_delay=timedelta(minutes=5),
        # Open-Meteo for 499 resorts takes ~10 minutes. The timeout is a backstop against
        # a hung connection holding the slot forever, not a performance target.
        execution_timeout=timedelta(minutes=45),
    )
    def weather() -> int:
        """Open-Meteo -> raw.weather. Cached per batch on disk, so a retry is cheap."""
        from alpine.weather import load_weather
        return load_weather()

    # -------------------------------------------------------------------- transform
    # BashOperator rather than a Python task: dbt's supported interface is its CLI, and
    # calling into dbt-core's Python internals means owning their API stability. The
    # non-zero exit code from a failed test is exactly the signal Airflow wants.
    #
    # This one task is really 101 assertions. If any of the 90 data tests fails, dbt
    # exits non-zero, this task goes red, and `train` never runs — so a model is never
    # fitted on a warehouse that failed its own quality checks. That is the reason the
    # dependency arrow points this way.
    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"cd {REPO / 'dbt'} && {DBT_BIN} build",
    )

    # ------------------------------------------------------------------ model + ship
    @task
    def train() -> dict:
        """Baselines, models, ablation -> metrics.json + the .joblib artefact."""
        from alpine.model import run
        summary = run()
        # Returned values are pushed to XCom, Airflow's small key-value store for passing
        # data between tasks. Metrics are a legitimate use — they are a handful of floats
        # and they show up in the UI, so a run's scores are inspectable without SSH.
        # XCom is emphatically not for DataFrames: it goes in the metadata database.
        return {
            "n_resorts": summary["n_resorts"],
            "best_model_mae": min(m["mae"] for m in summary["models"]),
            "snow_ablation_delta": summary["snow_ablation_mae_delta"],
        }

    @task
    def publish() -> str:
        """Freeze the marts into site/data.json for the static frontend."""
        from alpine.publish import publish as do_publish
        return str(do_publish())

    # ------------------------------------------------------------------ the graph
    # Written as an explicit chain. `weather` genuinely depends on `seed` — it reads the
    # resort coordinates out of `raw.resorts` to know which points to request — so this
    # ordering is a real data dependency, not just a tidy sequence.
    seed() >> weather() >> dbt_build >> train() >> publish()


alpine_pipeline()


# ======================================================================================
# THE VERSION THAT WOULD ACTUALLY NEED AN ORCHESTRATOR
# ======================================================================================
# If this ran against the current season instead of a frozen 2022 snapshot:
#
#   schedule="0 6 * * *"        every morning at 06:00
#   catchup=True                so a week of downtime backfills the missed days
#
#   seed          -> weekly at most; the resort dimension barely changes
#   weather       -> fetch only {{ data_interval_start }} .. {{ data_interval_end }},
#                    appending one day rather than refetching 365. This is the change
#                    that makes the whole thing incremental, and it is why Airflow
#                    templates the interval into every task.
#   dbt_build     -> `dbt build --select state:modified+` to rebuild only what changed
#   train         -> weekly, not daily. A model refit on one extra day of weather is
#                    churn: the metrics move by noise and nobody can tell a real
#                    regression from a reshuffled fold.
#   publish       -> daily, so the site tracks the warehouse.
#
# The useful observation is that the *shape* of the DAG does not change at all. What
# changes is the cadence of each node and the window of data each one touches — which is
# precisely the thing a Makefile has no way to express.
