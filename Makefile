.PHONY: setup seed profile weather build test clean

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

test:
	python -m pytest -q

clean:
	rm -f data/warehouse.duckdb data/warehouse.duckdb.wal
