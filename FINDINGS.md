# Findings — what drives the price of a ski pass?

Output of `make model`, interpreted. 422 model-ready resorts.

---

## The headline

> **Snow reliability does not command a price premium.** The apparent relationship is
> entirely explained by *which country the resort is in*.

That is a null result, and it is the correct answer rather than a disappointing one.

---

## 1. Baselines first

A score with nothing to compare it against is not a result. Three baselines, hardest last:

| baseline | MAE (EUR) | R² | |
|---|---|---|---|
| predict the global mean | 13.10 ± 1.14 | −0.011 | the floor |
| predict from terrain alone (ridge) | 10.99 ± 0.75 | 0.328 | size alone gets you a third of the way |
| **predict the country median** | **8.66 ± 0.84** | **0.520** | **the one that matters** |

The country median is a hard baseline on purpose: it is what any person familiar with the
domain would guess, and it already explains **52% of the variance**.

## 2. The models

| model | MAE (EUR) | R² |
|---|---|---|
| ridge, all features | 7.68 ± 0.34 | 0.640 |
| **gradient boosting, all features** | **6.62 ± 1.11** | **0.730** |

The best model beats the best baseline by **EUR 2.04 MAE — a 23.6% improvement**. Stated
that way rather than as a bare R², because "0.73" means nothing without knowing that
guessing the country median already gets you 0.52.

Note the *spreads*, and note that they point the other way from the means. The GBM is more
accurate on average but its fold-to-fold standard deviation (±1.11) is three times the
ridge's (±0.34) — it is the less stable of the two. At 422 rows that is what a more flexible
model buys you, and it is the reason "lowest mean score wins" is a bad selection rule.

---

## 3. The question: does snow reliability add anything?

Answered by **ablation** — two identical pipelines, one with the snow features, one without.
A coefficient describes one model's internal arithmetic; an ablation tests whether the
information helps at all.

| | MAE (EUR) | R² |
|---|---|---|
| ridge **without** snow features | **7.50 ± 0.53** | 0.646 |
| ridge **with** snow features | 7.68 ± 0.34 | 0.640 |

**Adding snow information makes the model very slightly worse.** The difference is −0.18
EUR against a fold-to-fold spread of ±0.53, so the honest reading is: **no effect,
indistinguishable from noise.**

Permutation importance says the same thing. Shuffling each feature and measuring the MAE
lost:

| feature | EUR of MAE lost | ± |
|---|---|---|
| **country** | **4.74** | 0.46 |
| total_slopes_km | 1.12 | 0.31 |
| gondola_lifts | 0.34 | 0.24 |
| lift_capacity_per_hour | 0.31 | 0.13 |
| lowest_point_m | 0.31 | 0.15 |
| … | | |
| snow_cover_pct_in_season | 0.13 | 0.14 |

**Country matters four times more than the next feature and thirty-five times more than
snow.** The first snow feature appears eleventh — and its importance (0.13) is smaller than
its own uncertainty (±0.14), which is the formal way of saying it is indistinguishable from
zero.

---

## 4. But the raw numbers *did* show a premium — so what happened?

They did, and it looked convincing:

| snow reliability | resorts | avg price |
|---|---|---|
| high (≥80% cover in season) | 220 | **EUR 51.00** |
| low (<80%) | 202 | EUR 45.20 |

A EUR 5.80 gap. Run the identical comparison **within each country** and it falls apart:

| country | n | high snow | low snow | premium |
|---|---|---|---|---|
| France | 72 | 41.9 | 36.0 | **+5.9** |
| Italy | 40 | 45.2 | 40.5 | **+4.8** |
| United States | 51 | 81.0 | 78.4 | +2.6 |
| Austria | 86 | 42.9 | 45.6 | **−2.7** |
| Switzerland | 58 | 51.5 | 56.0 | **−4.5** |
| Canada | 19 | 59.1 | 64.6 | **−5.5** |
| Germany | 19 | 25.0 | 31.3 | **−6.3** |

**The premium changes sign.** Positive in three countries, negative in four. There is no
consistent within-country effect at all.

### Why the aggregate is misleading

Because expensive countries happen to have snowier resorts:

| country | avg price | avg snow cover |
|---|---|---|
| United States | 80.0 | 79.9% |
| Canada | 60.5 | 89.1% |
| Switzerland | 53.2 | 80.8% |
| Austria | 44.3 | 76.5% |
| Italy | 42.6 | 68.2% |
| France | 39.0 | 68.0% |
| Germany | 30.0 | 66.7% |

The US and Canada are both high-price and high-snow. France and Germany are both
lower-price and lower-snow. Pool them and snow looks like it predicts price; it is really
standing in for the country.

**This is Simpson's paradox** — a relationship that holds in aggregate and reverses within
subgroups. It is the single most useful thing this project found, and it is exactly the
error the pipeline was built to avoid making silently.

---

## 5. What actually drives price

1. **Country, overwhelmingly.** Four times the importance of anything else. Lift-pass
   prices are set by local cost base, wage levels, purchasing power and currency — not by
   mountain conditions.
2. **Size.** Total piste kilometres, then terrain difficulty mix.
3. **Altitude and lift capacity**, modestly.
4. **Snow: no measurable effect** once the above are controlled for.

The economics make sense. A resort prices against its local competitors and its own cost
base. Snow reliability may well affect *how many* passes it sells — but that is a demand
question, and demand data is exactly what does not exist publicly.

---

## 6. What would change the answer

Honest limitations, in order of how much they matter:

- **One season of snow data (2022).** Reliability is a multi-year property; a single year
  is closer to weather than to climate. Ten years would make the feature meaningful.
- **No transactional data.** This models *list price*, not revenue, occupancy or realised
  price. A resort with poor snow may discount heavily — invisible here. **This is the gap
  a company like Pricenow does not have**, and it is what turns "what explains price" into
  "what should the price be tomorrow".
- **Price currency is undocumented** in the source; EUR assumed. If some rows are in local
  currency, the country effect is partly a units artefact — which would be a data problem
  masquerading as an economic finding.
- **38 resorts excluded** for suspect coordinates, 22 of them American. Since the US is the
  highest-price country, that exclusion is not random and could bias the country effect.
- **422 rows.** Enough for this, not enough for interactions.

---

## The version to say out loud

> *"The raw data showed snow-reliable resorts charging about 13% more, which looks like a
> clean answer. But splitting it by country, the premium reverses sign — positive in France
> and Italy, negative in Switzerland, Austria and Germany. It's Simpson's paradox: the
> expensive countries happen to be the snowy ones. Once country is controlled for, snow adds
> nothing — the ablation actually makes the model marginally worse, well inside the
> fold-to-fold noise. What does predict price is country first, by a factor of four, then
> size. Which makes economic sense: a resort prices against its local market, not against
> the weather. Snow probably drives how many passes it sells, and that's demand data I
> didn't have."*
