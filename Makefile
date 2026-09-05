.PHONY: setup seed profile weather build model publish serve verify airflow pipeline test clean

setup:
	pip install -r requirements.txt

## Step 2 — load the Kaggle CSVs into the warehouse (raw schema)
seed:
	python -m alpine.cli seed

## Step 3 — profile the raw data before trusting any of it
profile:
	python -m alpine.cli profile

## Step 5 — fetch weather from Open-Meteo for every resort
weather:
	python -m alpine.cli weather

## Step 9 — baselines, models, and the snow ablation
model:
	mkdir -p models
	python -m alpine.cli model

## Step 11 — freeze the marts into site/data.json so the page works without a backend.
##            Commit the result: it is what GitHub Pages deploys.
publish:
	python -m alpine.cli publish

## Step 10 — the API. --reload picks up edits without a restart; never use it in a container.
serve:
	python -m uvicorn alpine.serve:app --reload --port 8000

## Step 10 — end-to-end: prerequisites, then every endpoint against a real server
verify:
	bash scripts/verify.sh

## Every step in order, without an orchestrator. The DAG's non-Airflow equivalent.
pipeline:
	python -m alpine.cli pipeline

## Step 10.5 — Airflow, all components in one process, state kept inside the repo.
##             LOAD_EXAMPLES=False or ~50 tutorial DAGs bury this one in the UI.
##             Needs .venv-airflow — see airflow/README.md.
airflow:
	AIRFLOW_HOME=$(CURDIR)/airflow \
	AIRFLOW__CORE__LOAD_EXAMPLES=False \
	.venv-airflow/bin/airflow standalone

test:
	python -m pytest -q

clean:
	rm -f data/warehouse.duckdb data/warehouse.duckdb.wal
