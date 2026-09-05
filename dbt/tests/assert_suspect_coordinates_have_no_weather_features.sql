-- Singular test: a resort with suspect coordinates must have NULL weather features.
--
-- This is the decision from PROFILE.md §7, enforced rather than trusted. The weather for
-- those 38 resorts describes a location up to 2,000 km away; keeping the number and merely
-- flagging it would leave a plausible, confident, wrong value where a model would happily
-- use it.
--
-- A singular test rather than a generic one because it asserts a relationship *between*
-- columns, which is not something a column-level test can express.

select
    resort_id,
    resort_name,
    coordinates_suspect,
    snow_cover_pct_in_season,
    season_snowfall_cm

from {{ ref('mart_resort_pricing') }}

where coordinates_suspect
  and (snow_cover_pct_in_season is not null
    or season_snowfall_cm is not null
    or pct_season_days_freezing is not null)
