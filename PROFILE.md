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

15 rows carry unrecoverable `?` damage. We flag them in staging rather than pretend to fix them.

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
