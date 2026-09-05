# Alpine

**Does snow reliability command a price premium at ski resorts?**

An end-to-end ELT pipeline over three real sources, built to answer one question honestly.
The answer is no — and the interesting part is *why* it first looked like yes.

**[Live site →](https://justfeelix.github.io/alpine/)** · [Findings](FINDINGS.md) ·
[Design](DESIGN.md) · [Data profiling](PROFILE.md)

---

## The finding

Pooled across 422 resorts, snow-reliable resorts charge about **13% more** — €51.04 against
€45.22. That looks like a clean answer.

Split the identical comparison **by country** and the premium changes sign: positive in
France and Italy, negative in Austria, Switzerland, Canada, Germany and the US. There is no
consistent within-country effect at all.

This is **Simpson's paradox**. The expensive countries happen to be the snowy ones, so
pooled, snow stands in for the country. Three independent checks agree once country is
controlled for:

| check | result |
|---|---|
| ablation — model trained with vs without the 5 snow features | **−€0.18** MAE, against ±€0.53 fold noise |
| permutation importance | country **€4.74**, first snow feature **€0.13** (eleventh) |
| within-country premium | **+5.9** France … **−6.3** Germany — no consistent sign |

A ski pass is priced by *where it is*, not by *how snowy it is*. Which makes economic
sense: a resort prices against its local cost base and its local competitors. Snow may well
drive how many passes it *sells* — but that is a demand question, and demand data is
exactly what is not public.

---

## Architecture

```
Kaggle resorts.csv (cp1252)  ─┐
NASA NEO snow raster          ├─►  DuckDB raw  ─►  dbt  ─►  scikit-learn  ─►  FastAPI  ─►  page
Open-Meteo ERA5 archive (API)─┘                     │                            │
                                        staging ─ intermediate ─ marts     data.json ─► GitHub Pages
```

| layer | responsibility | key point |
|---|---|---|
| `alpine/seed.py` | CSVs → `raw` | decodes cp1252; DuckDB refuses the file, and latin-1 mangles 156 names |
| `alpine/weather.py` | Open-Meteo → `raw.weather` | Open-Meteo bills by *data volume*, not requests — see below |
| `dbt/staging` | typing, renaming, no logic | views: always consistent with raw |
| `dbt/intermediate` | season parsing, spatial join, coordinate validation | where every hard decision lives |
| `dbt/marts` | `dim_resort`, `fct_resort_weather_daily`, `mart_resort_pricing` | tables; the only thing downstream reads |
| `alpine/model.py` | baselines → models → ablation | baselines *first*, on purpose |
| `alpine/serve.py` | FastAPI | one dependency: `mart_resort_pricing` |
| `site/index.html` | the page | works with the API or from a frozen bundle |

**11 dbt models, 90 data tests, 2 singular tests, 4 macros.**

---

## Four things that went wrong, and what they taught

**The source file is cp1252, not UTF-8.** DuckDB refuses it outright. latin-1 "works" and
silently corrupts 156 resort names — byte `0x96` is an en-dash in cp1252 and a control
character in latin-1. *A decode that succeeds is not a decode that is correct.*

**HTTP 429 from Open-Meteo on the second batch.** It bills by data volume:
`weight ≈ ceil(days/14) × (variables/10) × locations`. 60 locations × 365 days × 4
variables ≈ 624 weighted calls against a 600/min limit. Fixed by shrinking batches,
dropping a variable, and printing the estimated budget *before* spending it.

**A `LIMIT` in an exploratory query hid a whole category of data — twice.** First a season
format (`"November - May, June - August"`, 4 rows), then the true count of encoding-damaged
rows (156, reported as 15 because the diagnostic query had `LIMIT 15` and I returned
`len(result)`). *Profiling queries must count and sample separately.*

**38 resorts have coordinates pointing somewhere else entirely.** Found by cross-checking
each stated base elevation against the elevation of its ERA5 grid cell — an error invisible
to any check run against the source file alone. Their weather columns are set to NULL
rather than describing the wrong mountain, because *a plausible wrong number is more
dangerous than a missing one*.

Full detail in [PROFILE.md](PROFILE.md).

---

## The test that the whole warehouse is arranged around

86 grid cells contain more than one resort, so joining resorts to the snow raster in the
wrong direction fans 499 rows out to 797,269. The raster is aggregated to one row per cell
*before* the join, making fan-out structurally impossible — and a custom generic test
asserts the row count still matches `stg_resorts`:

```sql
{% test row_count_matches(model, compare_model) %}
with this_model  as (select count(*) as n from {{ model }}),
     other_model as (select count(*) as n from {{ compare_model }})
select this_model.n as actual_rows, other_model.n as expected_rows
from this_model cross join other_model
where this_model.n != other_model.n
{% endtest %}
```

It was verified by deliberately breaking the join and watching it fail.

---

## Running it

```bash
pip install -r requirements.txt
cd dbt && dbt deps && cd ..

make seed        # CSVs -> raw
make profile     # profile before trusting anything
make weather     # Open-Meteo -> raw.weather  (~10 min, respects the rate limit)
cd dbt && dbt build && cd ..     # 11 models, 90 tests
make model       # baselines, models, ablation -> models/metrics.json + the .joblib
make publish     # freeze the marts -> site/data.json
make serve       # http://localhost:8000
make verify      # 25 end-to-end checks
```

`snow.csv` is 26 MB and gitignored — download the
[Kaggle ski-resort dataset](https://www.kaggle.com/datasets/ulrikthygepedersen/ski-resorts)
into `data/raw/ski-resort-data/`.

### The API

| endpoint | |
|---|---|
| `GET /health` | reports `model_loaded` explicitly — a health check that ignores the model is worthless |
| `GET /resorts` | filter by country / price / snow, paginated |
| `GET /resorts/{id}` | the row, plus predicted price and residual |
| `GET /countries` | price and snow side by side — the confound, as data |
| `GET /model` | cross-validated metrics and lift over baseline |
| `POST /predict` | price a hypothetical resort; only `country` is required |
| `GET /predictions/missing-price` | the 9 resorts with no price in the source |

---

## Why the site works without a backend

GitHub Pages serves static files and will not run Python. Rather than hosting the API or
maintaining a second cut-down frontend, `make publish` freezes the marts into
`site/data.json` and the page renders from that. It also probes the relative URL `health`:
served by FastAPI that resolves to `/health` and answers, so the prediction panel unlocks;
on Pages it 404s and the page falls back to the snapshot. **One HTML file, no build step.**

---

## Honest limitations

- **One season of snow data (2022).** Reliability is a multi-year property; a single year is
  closer to weather than to climate. "We found no effect" and "there is no effect" are
  different claims, and this evidence only supports the first.
- **List price only** — no occupancy, discounting or realised revenue. A resort with poor
  snow may discount heavily, and that is invisible here.
- **Source currency is undocumented**; EUR assumed. If some rows are in local currency, part
  of the country effect is a units artefact.
- **38 excluded resorts are not a random sample** — 22 are American, and the US is the
  highest-price country.
- **422 rows.** Enough for main effects, not for interactions.
