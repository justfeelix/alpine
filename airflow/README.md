# Airflow

**No account, no signup, no cloud service.** Airflow is open-source software you install
and run yourself, like Postgres. `pip install apache-airflow`, start it, and it serves a
web UI on your own machine.

## What is actually running

Starting Airflow starts several processes at once:

| component | job |
|---|---|
| **scheduler** | decides which task should run next and hands it to an executor |
| **dag processor** | re-reads the `dags/` folder every few seconds and picks up edits |
| **api server** | the web UI and REST API — this is what you open in a browser |
| **metadata database** | every run, task state, log pointer and XCom value. SQLite by default |
| **executor** | actually runs the task. `LocalExecutor` = a subprocess on this machine |

In production those run on separate machines with Postgres as the metadata DB and Celery
or Kubernetes as the executor. Locally, `airflow standalone` runs all of them in one
process with SQLite — same concepts, one terminal.

## Install

Airflow gets **its own virtualenv**. It pins a large dependency tree, and merging that
with the project's is a fight worth avoiding — which is also why the DAG resolves the
`dbt` binary by path rather than assuming it is importable.

```bash
cd ~/Desktop/Local/Life/alpine

python3 -m venv .venv-airflow
source .venv-airflow/bin/activate

# The constraints file pins Airflow's ~600 transitive dependencies to a combination
# that is known to work. Installing without it usually works and occasionally produces
# a resolver puzzle that takes an afternoon.
AF=3.1.8; PY=$(python -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
pip install "apache-airflow==$AF" \
  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-$AF/constraints-$PY.txt"

# The DAG imports the alpine package, so its runtime dependencies need to be here too.
# Verified to coexist with Airflow without conflicts.
#
# Install the versions the PROJECT venv already has, not whatever pip resolves today —
# see "The gotcha two virtualenvs create" below for why this matters. Adjust the path if
# your project venv lives somewhere else; here it sits beside the repo, not inside it.
PROJECT_VENV=../.venv-1
$PROJECT_VENV/bin/pip freeze \
  | grep -iE "^(duckdb|pandas|scikit-learn|numpy|scipy|httpx|joblib)==" \
  | xargs pip install
```

To check they agree afterwards:

```bash
diff <(../.venv-1/bin/pip freeze | grep -iE "^(scikit-learn|joblib|pandas|numpy)==") \
     <(pip freeze              | grep -iE "^(scikit-learn|joblib|pandas|numpy)==") \
  && echo "environments agree"
```

## Run

```bash
source .venv-airflow/bin/activate
export AIRFLOW_HOME=$(pwd)/airflow          # keeps all state inside the repo, gitignored
export AIRFLOW__CORE__LOAD_EXAMPLES=False   # otherwise ~50 tutorial DAGs clutter the UI

airflow standalone
```

Or just `make airflow`, which sets those three lines for you.

On first start it creates the metadata database and prints a generated admin password:

```
Login with username: admin  password: <printed here, also in simple_auth_manager_passwords.json.generated>
```

Open **http://localhost:8080**.

> Airflow's UI and the Alpine API both default to port 8080 vs 8000 — no clash, but do not
> start Airflow on 8000.

## What to do in the UI

1. **DAGs list** → `alpine_pipeline`. It starts **paused**; that is deliberate, so nothing
   runs the instant you install it.
2. Click the DAG → **Graph** view. Five nodes:
   `seed → weather → dbt_build → train → publish`.
3. Hit **Trigger** (▶). Watch nodes go from white to light green (running) to dark green.
4. Click any node → **Logs**. This is the actual payoff: the stdout of that task, kept and
   attributable to a specific run, weeks later.
5. Click `train` → **XCom**. The metrics it returned are stored there, so a run's scores
   are inspectable without going near the filesystem.

Useful views once you have more than one run: **Grid** (runs as columns, tasks as rows —
a failed task is a red square you click straight into) and **Duration** (spot a task
getting slower before it starts timing out).

## Trying the parts that matter

The interesting behaviour is failure handling, and you can provoke it safely:

**Watch a retry.** Turn off your wifi and trigger the DAG. `weather` fails, goes
*up_for_retry* (yellow), waits 5 minutes, tries again — 3 times before failing for real.
Compare with `seed`, which inherits the DAG default of 1 retry after 1 minute. The API
task has its own policy because it is the one that talks to a rate-limited service.

**Watch a failure stop the pipeline.** Break a dbt test on purpose — change an
`accepted_range` in `dbt/models/**/*.yml` to something impossible. `dbt_build` exits
non-zero, goes red, and `train` and `publish` are marked *upstream_failed* and never run.
**A model is never fitted on a warehouse that failed its own quality checks** — that is
what the arrow between those two nodes buys you.

**Watch resumption.** After fixing it, use **Clear** on the failed task rather than
re-triggering. Airflow reruns only that task and everything downstream — `seed` and
`weather` are not repeated, so you do not spend another 5,400 Open-Meteo credits to
recover from a typo.

## Testing a single task without any of this

```bash
airflow tasks test alpine_pipeline publish
```

Runs one task in the foreground, prints its logs, and records nothing. This is the fast
loop while writing a DAG — no scheduler, no trigger, no UI.

## Command line

```bash
airflow dags list                    # what Airflow can see
airflow dags list-import-errors      # why it cannot see your DAG (usually the answer)
airflow tasks list alpine_pipeline
airflow dags trigger alpine_pipeline
airflow dags reserialize             # force a re-read of the dags folder
```

If a DAG does not appear in the UI, `list-import-errors` is almost always the answer: a
Python exception at import time means the file is silently skipped.

## The gotcha two virtualenvs create

Airflow's venv and the project's venv are separate on purpose — but `train` writes a
pickled model that `make serve` then has to read. **The artefact crosses the boundary
between the two environments**, and if their library versions differ it will not load:

```
ModuleNotFoundError: No module named 'dill'
```

That is what happens when joblib 1.6.0 (Airflow's venv) writes a pickle and joblib 1.5.3
(the project's) tries to read it. Same scikit-learn on both sides — it was joblib that
differed, which is why the artefact now stamps *both* versions and `scripts/verify.sh`
prints a warning when either disagrees.

Two ways to avoid it:

- pin the same `scikit-learn` and `joblib` in both virtualenvs (simplest), or
- run `train` from the project venv and let Airflow only orchestrate — in production this
  is what `ExternalPythonOperator`, `DockerOperator` and `KubernetesPodOperator` exist
  for: they run the task in the environment that owns it.

If you hit it, `make model` from the project venv regenerates the artefact and fixes it.

## Honest note

For this pipeline Airflow is over-engineered, and the DAG says so in its own docstring.
The source data is a frozen 2022 snapshot — `schedule=None` is the truthful setting
because there is nothing to schedule. Airflow earns its place when data arrives
incrementally; the bottom of `dags/alpine_pipeline.py` sketches what that version looks
like, and the shape of the graph does not change at all — only the cadence of each node
and the window of data it touches.

It did earn its keep once already: wiring it up exposed that every output path in the
project was relative, so the pipeline only worked when the working directory happened to
be the repo root. `make` always runs from there. A scheduler does not.
