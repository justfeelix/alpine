-- Singular test: every row marked model-ready must actually be usable.
--
-- `is_model_ready` is a promise made to the modelling layer. If a row carries the flag but
-- has a NULL target or NULL features, the promise is broken and the model would silently
-- train on fewer rows than it reported — or crash, depending on the estimator.
--
-- Cheaper to assert here than to debug in a notebook.

select
    resort_id,
    resort_name,
    price_eur,
    snow_cover_pct_in_season,
    total_slopes_km,
    vertical_drop_m

from {{ ref('mart_resort_pricing') }}

where is_model_ready
  and (price_eur is null
    or snow_cover_pct_in_season is null
    or total_slopes_km is null
    or vertical_drop_m is null)
