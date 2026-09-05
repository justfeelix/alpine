# Profiling findings — what the raw data actually is

Output of `make profile`, interpreted. Run before writing a single transformation.

**Nothing here was assumed. Every line came from querying the raw files.**

---

## 1. The file isn't UTF-8

DuckDB refused it outright:

```
Invalid unicode (byte sequence mismatch) detected. This file is not utf-8 encoded.
```

It's **cp1252** — a Windows/Excel export. Determined by testing candidates, not guessing:
byte `0x96` decodes to an en-dash in cp1252 and to an **undefined control character** in
latin-1. So "latin-1 nearly works" is a trap — it decodes without error and silently mangles
23 resort names.

**Two kinds of damage, and only one is fixable:**

| kind | example | fixable? |
|---|---|---|
| Wrong decoder | `Espace San Bernardo \x96 La Rosière` | ✅ decode as cp1252 |
| Already destroyed upstream | `Nevados de Chilla?n`, `Val d'Ise?re` | ❌ the `?` is *in the file* |

**156 rows** carry unrecoverable `?` damage — not 15, which is what this document said until
the staging layer disagreed with it. The profiling query used `LIMIT 15` and then reported the
length of the result as the count. A `LIMIT` on a diagnostic query caps the *evidence*, not the
problem. Fixed in `profile.py`; the count and the sample are now separate queries.

The damage comes in two kinds, and the distinction is useful:

| kind | count | example | matters? |
|---|---|---|---|
| **Destroyed letter** | 88 | `Chilla?n`, `Val d'Ise?re`, `Cha?tel` | yes — the name is misspelt |
| **Destroyed separator** | ~100 | `La Rosière/?La Thuile`, `Balme-?Les Autannes` | cosmetic — a dash or slash |

`stg_resorts` flags them separately, because a misspelt resort name breaks name matching against
any other source, while a mangled separator does not.

---

## 2. There are no NULLs — and that's the problem

```
Nulls: none, in any column
```

That is not good news. **Missingness is encoded as `0`.** Nine resorts have `Price = 0`:

| resort | country | slopes | lifts |
|---|---|---|---|
| Perisher | Australia | 100 km | 49 |
| Yellowstone Club | United States | 80 km | 15 |
| Pragelato | Italy | 50 km | 5 |
| Uludağ-Bursa | Turkey | 28 km | 16 |
| … 5 more | | | |

Perisher is Australia's largest resort and the Yellowstone Club is a private members' club with
a six-figure joining fee. **Neither is free.** `0` means "we didn't have this number."

**This is the single most important finding**, because a price model trained with nine zeros in
the target learns that big resorts are sometimes free. They must become `NULL` and be excluded
from training — not imputed, because we have no basis for a value.

### Other ambiguous zeros

| column | count | verdict |
|---|---|---|
| `Snow cannons = 0` | 226 (45%) | **probably genuine** — many small resorts have none |
| `Longest run = 0` | 212 (42%) | **probably missing** — every resort has a longest run |
| `Lift capacity = 0` | 3 | missing |
| `Total lifts = 0` | 1 | missing — Mzaar Kfardebian has 80 km of piste and "0" lifts |

The honest treatment differs per column, and saying *why* is the point. `Snow cannons = 0` is
plausibly real; `Total lifts = 0` on an 80 km resort is not.

---

## 3. Internal consistency: clean

```
slopes_mismatch  lifts_mismatch  elevation_inverted  null_ids  duplicate_ids
              0               0                   0         0              0
```

Beginner + intermediate + difficult **always** equals total slopes. Surface + chair + gondola
always equals total lifts. Highest is always above lowest. IDs are unique.

These still go in as dbt tests — not to find today's bug, but to catch tomorrow's when the
source refreshes.

---

## 4. Season strings need a real parser

| value | n | case |
|---|---|---|
| `December - April` | 220 | wraps the year boundary |
| `November - April` | 110 | wraps |
| `December - March` | 40 | wraps |
| `November - May` | 35 | wraps |
| **`Unknown`** | **27** | no data |
| `June - October` | 10 | southern hemisphere, no wrap |
| `April` | 5 | **single month, no dash** |
| `Year-round` | 4 | **not a range at all** |

Four distinct shapes: `X - Y`, single month, `Year-round`, `Unknown`. A naive `split(' - ')`
breaks on three of them.

**And the wrap is the real trap.** For `December - April`, start month = 12 and end month = 4,
so `month BETWEEN 12 AND 4` matches **nothing** — no error, just an empty result. The fix:

```sql
(start <= end AND month BETWEEN start AND end)
OR (start >  end AND (month >= start OR month <= end))
```

### Hemisphere cross-check

| hemisphere | resorts | season starts Jun/Jul |
|---|---|---|
| northern | 482 | 12 |
| southern | 17 | **16** |

16 of 17 southern resorts have a southern-winter season — the data is broadly right. The
1 that doesn't, and the 12 northern resorts with summer seasons (glacier skiing), are worth
looking at rather than assuming.

---

## 5. The snow grid is exactly as assumed — and the join has two real problems

Grid verified: **0.25° spacing, centres at .125/.375/.625/.875, zero cells off-grid.**
820,522 rows, 135,939 distinct cells, 12 monthly snapshots of 2022, snow cover 0.39–100%.

Snapping resorts to cells:

```
n_resorts  n_distinct_cells  resorts_with_no_snow_data
      499               369                        22
```

**Problem 1 — 86 cells contain more than one resort (216 resorts affected).** At 0.25° a cell
is ~25 km, so neighbouring resorts in the same valley share one. The resort→cell join is
**many-to-one**, which is safe: each resort still gets exactly one cell. But if the join were
ever written backwards, 216 resorts would fan out. **This is precisely what the row-count
canary test guards.**

**Problem 2 — 22 resorts (4.4%) have no snow cell at all.** Their grid square isn't in the
NEO product, most likely masked as water or missing. Options: drop them, or keep them with
`NULL` snow and a `has_snow_data` flag.

**We keep them.** Dropping 22 resorts silently shrinks the population and biases anything
computed afterwards. A flagged NULL is honest and lets the model decide.

---

## 6. Consequences for the design

| finding | what changes |
|---|---|
| cp1252 encoding | decode in extract; `?` rows flagged, not "fixed" |
| `Price = 0` means missing | → `NULL` in staging; **excluded from model training** |
| `Total lifts = 0`, `Longest run = 0` | → `NULL`; `Snow cannons = 0` kept as real |
| 4 season formats + year wrap | dedicated `int_resort_season` model with the wrap-aware predicate |
| 86 shared cells | resort→cell join is many-to-one; row-count canary is mandatory |
| 22 resorts without snow | keep with `has_snow_data = false`; never silently dropped |
| Lat/lon verified in range | Open-Meteo can be called safely for all 499 |

---

## The one-sentence version, for Monday

> *"Before writing any transformation I profiled the raw data, and it changed the design three
> times: the file was cp1252 rather than UTF-8, missing values were encoded as zero — including
> nine resorts priced at zero that are certainly not free — and the season strings wrap the year
> boundary, so a naive BETWEEN returns no rows and no error."*

---

# 7. The weather API found a bug in the dataset

*Added after step 5. This was not something the Kaggle files could reveal on their own — it
took an independent source to see it.*

Open-Meteo returns the **elevation of the grid cell** it served. Comparing that against each
resort's stated base elevation was meant to be a cheap sanity check. It found something worse
than expected.

## The signal

| resort | Kaggle coordinates | actual location | error |
|---|---|---|---|
| Arapahoe Basin | 40.121, −80.670 | 39.642, −105.872 (Colorado) | **2,144 km** |
| Wolf Creek | 42.696, −123.395 | 37.472, −106.793 (Colorado) | **1,524 km** |
| Eldora Mountain | 37.386, −122.073 | 39.937, −105.583 (Colorado) | **1,457 km** |
| Alta | 42.674, −95.304 | 40.588, −111.638 (Utah) | **1,375 km** |
| Keystone | 41.999, −92.198 | 39.605, −105.943 (Colorado) | **1,186 km** |

These are **geocoding failures from name collisions**. There is a Keystone in Iowa, an Alta in
Iowa, an Eldora in California. Whoever built the dataset geocoded resort names and got the
wrong place — and nothing downstream ever noticed, because a plausible latitude and longitude
looks exactly like a correct one.

## How to detect it

The tell is the **elevation gap**: source base elevation minus ERA5 grid elevation.

```
     p05     p25     p50    p75     p95     max
 -1307.0  -629.0  -162.0    0.0  1150.0  2909.0
```

**Negative gaps are normal and expected.** ERA5 is a ~25 km grid, so it smooths terrain: a
cell containing an alpine valley averages *above* the valley floor. Switzerland's mean gap is
−678 m, which is the physics working correctly, not an error.

**Large positive gaps are the anomaly.** A resort claiming a base 500 m *above* its own grid
cell is claiming to be on a mountain that the terrain model says isn't there.

| threshold | resorts | reading |
|---|---|---|
| gap > 1000 m | **29** | almost certainly wrong coordinates |
| gap > 500 m | **38** | suspect, worth review |

By country, `gap > 500 m`: **United States 22 of 78 (28%)**, France 6, Japan 2, Germany 2,
Austria 2, and one each in Chile, Canada, Italy, New Zealand. The concentration in the US is
consistent with the cause — a large country with many duplicated place names.

## What this changes

**The weather for those 38 resorts describes the wrong place.** Snowfall for a cell in Iowa
tells you nothing about Alta, Utah. This is not a rounding problem; the data is simply about
somewhere else.

Decisions:

1. **Flag, don't silently drop.** `dim_resort` gets `elevation_gap_m` and
   `coordinates_suspect`. Dropping 38 resorts quietly would bias every aggregate afterwards
   and leave no trace of why.
2. **Exclude suspect resorts from anything weather-derived.** Snow-reliability metrics built
   on the wrong location are worse than a NULL, because they look usable.
3. **Keep them for the price model**, which uses terrain, capacity and country — none of which
   depend on coordinates being right.
4. **The threshold is a heuristic**, validated against five resorts whose real locations are
   independently verifiable. The US cases are unambiguous. The French and Austrian ones may be
   false positives from genuinely steep terrain, so they are flagged for review rather than
   treated as certainly wrong.

## Why this is the most useful thing in the profile

Every other finding here came from looking harder at the file. This one was **invisible from
inside the dataset** — the coordinates are well-formed, in range, and internally consistent.
It only surfaced by joining against an independent source that happened to carry a fact the
first source also implies.

That is the general lesson, and it is worth stating plainly:

> *"The coordinates passed every check I could run against the file itself — right type, right
> range, no nulls, no duplicates. They were still wrong by 2,000 km. I only found it because
> the weather API returns the elevation of the cell it served, and comparing that to the
> resort's own stated elevation gave me a second opinion. Validation against an independent
> source catches a class of error that no amount of internal consistency checking will."*
