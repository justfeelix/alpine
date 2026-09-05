# Alpine — design document

**Ski-resort pricing and snow reliability: a real ELT pipeline.**

This document is the plan *and* the explanation. Every section says what we're building, which tool
is responsible, and why it's done that way — so that on Monday you can talk about any layer without
guessing.

Read this before any code exists. Build order is at the end.

---

## 1. The question we're answering

> **What drives the price of a ski pass — and does snow reliability command a premium?**

That is the Kaggle dataset's own headline question, and it is close to Pricenow's actual business.
It's also answerable with data that genuinely exists, which matters more than it sounds.

Secondary questions the marts should support:

- Which resorts give the most piste kilometre per euro? (value ranking)
- Does lift capacity predict price better than terrain size?
- How does the price/snow relationship differ by country — is a Swiss franc premium real, or is it
  explained by altitude?
- Which resorts are most exposed to a warming climate? (low base elevation + high price)

---

## 2. What data we actually have

### 2.1 `resorts.csv` — the dimension

**Grain: one row per resort.** ~500 rows, 25 columns.

| column | type | notes |
|---|---|---|
| `ID` | int | primary key |
| `Resort` | text | name |
| `Latitude`, `Longitude` | float | **this is the join key to everything else** |
| `Country`, `Continent` | text | |
| `Price` | int | lift pass. **Currency not stated** — assume EUR, document the assumption |
| `Season` | text | `"November - May"` — a *string range*, needs parsing |
| `Highest point`, `Lowest point` | int | metres |
| `Beginner/Intermediate/Difficult/Total slopes` | int | km of piste |
| `Longest run` | int | km |
| `Snow cannons` | int | |
| `Surface/Chair/Gondola/Total lifts` | int | |
| `Lift capacity` | int | persons/hour |
| `Childfriendly`, `Snowparks`, `Nightskiing`, `Summer skiing` | `"Yes"`/`"No"` | strings, need casting |

### 2.2 `snow.csv` — the time series

**Grain: one row per 0.25° grid cell per month, 2022.** Global. Roughly a million rows.

| column | notes |
|---|---|
| `Month` | `2022-12-01` — monthly snapshot |
| `Latitude`, `Longitude` | **grid cell centre**, not a resort |
| `Snow` | snow cover %, 0–100 (NASA NEO MOD10C1) |

**This is the interesting part.** `snow.csv` is not keyed to resorts — it's a global raster. To use
it you have to **snap each resort's coordinates to its grid cell**:

```
cell_lat = floor(lat / 0.25) * 0.25 + 0.125
cell_lon = floor(lon / 0.25) * 0.25 + 0.125
```

(Grid centres sit at `.125`, `.375`, `.625`, `.875` — confirmed from the raw data.)

That's a **spatial join**, and it's a genuinely good thing to have done. It's also where the fan-out
risk lives: get the snapping wrong and one resort matches several cells, quietly multiplying every
subsequent aggregate.

### 2.3 Open-Meteo — the real API

**Grain: one row per resort per day.** This is what we fetch ourselves.

- Endpoint: `https://archive-api.open-meteo.com/v1/archive`
- **No API key, no signup.** Free for non-commercial use, **10,000 calls/day**
- Historical reanalysis (ERA5), daily aggregates: `temperature_2m_max/min`, `snowfall_sum`,
  `precipitation_sum`
- Input is `latitude` / `longitude` — **the resort coordinates are the join key**

This is the piece that turns a static Kaggle download into an actual data pipeline: an external
system, over the network, that can be slow, rate-limited, or down.

---

## 3. The honest gap — and why we're not papering over it

**There is no public ski-resort booking data.** Your research confirmed it: reservation logs are
revenue-critical and commercially sensitive, so nobody publishes them.

That means **no demand forecasting**. No booking curve, no lead time, no price elasticity — those
all need transactional data we don't have.

**The tempting move is to bolt on the Hotel Booking Demand dataset and join it to ski resorts. Don't.**
Those are hotel reservations with no connection to these resorts; joining them would be fabricating
a relationship. Maximilian would spot it, and inventing a join is exactly the failure mode you spend
the rest of the interview claiming you guard against.

**Say this instead, and it's a strong answer:**

> *"The one thing I couldn't get is transactional booking data — it doesn't exist publicly, for
> obvious commercial reasons. So I built what the data supports: a cross-sectional price model. The
> piece I'd add on day one at Pricenow is the booking log, because that's what turns this from
> 'what explains price' into 'what should the price be tomorrow' — and you already have it."*

That sentence demonstrates you understand what data the business needs and why, which is worth more
than a model built on a fabricated join.

---

## 4. Architecture — and what each tool is responsible for

```
  ┌─────────────────┐     ┌──────────────────┐
  │  resorts.csv    │     │   Open-Meteo     │   external API, network, retries
  │  snow.csv       │     │   /v1/archive    │
  │  (Kaggle, seed) │     └────────┬─────────┘
  └────────┬────────┘              │
           │                       │  Python: extract + load ONLY
           ▼                       ▼  (no business logic here)
  ┌──────────────────────────────────────────┐
  │  DuckDB  —  raw schema                    │   raw_resorts, raw_snow, raw_weather
  │  exactly as the source gave it            │
  └────────────────────┬─────────────────────┘
                       │  dbt: everything below this line is SQL
                       ▼
  ┌──────────────────────────────────────────┐
  │  staging   — one model per source         │   cast, rename, dedupe. no joins.
  │  stg_resorts, stg_snow, stg_weather       │
  └────────────────────┬─────────────────────┘
                       ▼
  ┌──────────────────────────────────────────┐
  │  intermediate — joins & business logic    │   spatial join, season parsing,
  │  int_resort_snow, int_resort_weather      │   snow-reliability metrics
  └────────────────────┬─────────────────────┘
                       ▼
  ┌──────────────────────────────────────────┐
  │  marts — what anyone actually queries     │   dim_resort, fct_resort_weather_daily,
  │                                           │   mart_resort_pricing
  └────────────────────┬─────────────────────┘
                       ▼
        ┌──────────────┴───────────────┐
        ▼                              ▼
  scikit-learn                    FastAPI + frontend
  price model                     serve it
```

### Who is responsible for what

| layer | tool | responsibility | explicitly **not** its job |
|---|---|---|---|
| **Extract** | Python (`httpx`) | Talk to the API. Retries, backoff, caching, rate limits. | Cleaning or reshaping data |
| **Load** | Python + DuckDB | Get raw bytes into the warehouse, idempotently | Interpreting the data |
| **Warehouse** | **DuckDB** (→ Snowflake) | Store; execute SQL fast | Deciding what the data means |
| **Transform** | **dbt** | All business logic, as SQL. Layering, lineage, docs | Fetching data |
| **Test** | **dbt tests** | Assert the data is what we claim | Fixing it |
| **Model** | **scikit-learn** | Predict price from resort attributes | Data cleaning (that's upstream) |
| **Serve** | **FastAPI** | Expose marts + predictions over HTTP | Business logic |
| **Ship** | **Docker** | Make it run identically anywhere | — |

**The single most important line in that table:** *extract does not transform.* Raw lands raw. Every
interpretation happens in dbt, in SQL, in version control, with tests. That's the "EL**T**" —
transform *after* loading, inside the warehouse.

**Why that matters, in one sentence you can say out loud:**

> *"If I clean on the way in and get it wrong, the original is gone. If I load raw and clean in dbt,
> I can change a definition and rebuild three months of history from source."*

---

## 5. The dbt layers, model by model

### Staging — `stg_*`

**One model per source table. Casting, renaming, deduplication. No joins, no business logic.**

| model | what it does |
|---|---|
| `stg_resorts` | snake_case the columns, `"Yes"/"No"` → boolean, fix the encoding damage (`Chilla?n`), cast numerics |
| `stg_snow` | parse `Month` to a date, keep grid coordinates as-is |
| `stg_weather` | cast dates, rename Open-Meteo's variable names to ours |

Staging is deliberately boring. Its value is that **everything downstream can assume clean types**,
and there is exactly one place where a source column name appears.

### Intermediate — `int_*`

**Where joins and business rules live.**

| model | what it does | the hard part |
|---|---|---|
| `int_resort_snow_cells` | snap each resort to its 0.25° grid cell | **must stay one row per resort** — otherwise fan-out |
| `int_resort_snow_monthly` | resort × month snow cover | grain changes here: state it |
| `int_resort_season` | parse `"November - May"` into start/end months | **the season wraps the year** — Nov(11) → May(5) is not `start <= end` |
| `int_snow_reliability` | per resort: mean/min snow cover **during its own season** | uses `int_resort_season`; a Southern-hemisphere resort's season is Jun–Sep |

`int_resort_season` is the one worth explaining on Monday. `"November - May"` spans a year boundary,
so a naive `month BETWEEN start AND end` returns **nothing**. The fix is
`(start <= end AND month BETWEEN start AND end) OR (start > end AND (month >= start OR month <= end))`.
That's a real modelling bug that produces empty results rather than an error.

### Marts — `dim_*`, `fct_*`, `mart_*`

| model | grain | contents |
|---|---|---|
| `dim_resort` | one row per resort | all attributes, cleaned, plus derived: `vertical_drop`, `piste_per_lift`, `price_per_piste_km` |
| `fct_resort_weather_daily` | one row per resort per day | Open-Meteo: temps, snowfall, precipitation |
| `mart_resort_pricing` | one row per resort | the modelling table: price + terrain + capacity + snow reliability + weather aggregates |

**Naming convention, and why it's not decoration:** `dim_` = descriptive, one row per thing.
`fct_` = events/measurements, many rows per thing. Stating the grain in the name means the next
person knows whether `SUM` is safe before they open the file.

---

## 6. Tests — the differentiator

The four dbt built-ins (`unique`, `not_null`, `accepted_values`, `relationships`) are table stakes.
**The ones worth writing are the business rules**, because those catch data that is present,
well-typed, and impossible.

| test | why | expect it to |
|---|---|---|
| `resort_id` unique + not_null | grain contract | pass |
| `total_slopes = beginner + intermediate + difficult` | derivable — the source should be internally consistent | **possibly fail** — worth finding out |
| `total_lifts = surface + chair + gondola` | same | **possibly fail** |
| `highest_point > lowest_point` | physically necessary | pass, probably |
| `price >= 0` | | pass |
| `snow_cover BETWEEN 0 AND 100` | it's a percentage | pass |
| `latitude BETWEEN -90 AND 90`, `longitude BETWEEN -180 AND 180` | | pass |
| southern-hemisphere resorts have southern seasons | `latitude < 0` should imply a Jun–Sep season | **interesting either way** |
| every resort in `mart_` appears in `dim_resort` | `relationships` — referential integrity | pass |
| row count of `dim_resort` == row count of `stg_resorts` | **fan-out canary** | must pass |

That last one is the important one. **If the spatial join fans out, this is what catches it.** Say
that on Monday.

---

## 7. The model

**Target:** `price` (lift pass, EUR)
**Grain:** one row per resort — cross-sectional, ~500 rows

**Features:** vertical drop, total piste km, share of difficult terrain, total lifts, lift capacity,
snow cannons, snow reliability (from `int_snow_reliability`), mean season snowfall (Open-Meteo),
country, continent, has-snowpark / night-skiing / summer-skiing.

**Method:**
1. **Baseline first** — predict the global mean, then the country median. *A model that can't beat
   "the median price in this country" is not worth having.*
2. Ridge with one-hot country inside a `Pipeline`
3. Gradient boosting for comparison
4. **Cross-validation, not a single split** — with 500 rows, a single 80/20 split is noise
5. Permutation importance on held-out data

**The interesting output isn't the score, it's the answer:** does snow reliability still predict
price once you control for altitude and size? If it doesn't, that's a real finding — the premium
people *think* they pay for snow may just be a premium for being high up.

⚠️ **This is cross-sectional, so it explains price — it does not forecast it.** Be precise about
that distinction on Monday; conflating the two is exactly the kind of thing that gets probed.

---

## 8. Frontend (after the pipeline works)

Not started until the pipeline is green. Planned:

- A single page served by FastAPI: resort table (sortable, filterable), a price-vs-snow scatter, a
  map, and a "what drives this resort's price" panel from the model
- Static HTML + a chart library, served from the same FastAPI app — **no separate frontend build**,
  because a second toolchain is a second thing that can break in front of him
- Endpoints: `/resorts`, `/resorts/{id}`, `/model`, `/predict`

---

## 9. Build order

Nothing is skipped. Each step is verifiable before the next.

| # | step | proves |
|---|---|---|
| 1 | Repo skeleton, `requirements.txt`, `Makefile` | — |
| 2 | Load `resorts.csv` + `snow.csv` into DuckDB raw | data lands |
| 3 | **Profile the raw data** — nulls, ranges, the encoding damage, do the slope sums add up | *we know what we actually have* |
| 4 | Open-Meteo client: retries, caching, batching | the API integration works |
| 5 | Fetch weather for all resorts → raw | real external data in the warehouse |
| 6 | dbt: staging + tests | types are clean |
| 7 | dbt: intermediate (spatial join, season parsing) | **the hard bit** |
| 8 | dbt: marts + business-rule tests | the fan-out canary passes |
| 9 | Model: baselines → ridge → GBM | honest comparison |
| 10 | FastAPI + `verify.sh` | it runs end to end |
| 11 | Frontend | it looks finished |
| 12 | Snowflake profile swap | the cloud story is real, not claimed |

**Step 3 is not optional.** Profiling before modelling is what separates someone who has processed
real data from someone who has read about it — and the encoding damage in `Nevados de Chilla?n` is
already visible in row 8 of the raw file.

---

## 10. Known unknowns

Things to verify while building, not assume:

- **Currency of `Price`** — undocumented. Check the range and compare against a known resort.
- **`Snow cannons = 0`** — genuinely zero, or missing?  Same for `Longest run = 0`.
- **Does Open-Meteo accept multiple coordinates per call?** If yes, ~500 resorts is a handful of
  requests. If not, it's 500 — still fine under a 10k/day limit, but slower and needs throttling.
- **`snow_depth` vs `snowfall_sum`** — depth is hourly-only in ERA5; the daily aggregate is
  snowfall. Use `snowfall_sum` and say so.
- **Do the slope/lift components sum to the totals?** Step 3 answers this.

---

## Sources

- [Ski Resorts dataset (Kaggle)](https://www.kaggle.com/datasets/ulrikthygepedersen/ski-resorts/data) — ski-resort-stats.com + NASA NEO, public domain
- [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)
- [Open-Meteo pricing / rate limits](https://open-meteo.com/en/pricing) — free non-commercial, 10,000 calls/day
