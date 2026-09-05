-- Snow cover per resort, measured only during that resort's OWN season.
--
-- Grain: one row per resort.
--
-- --------------------------------------------------------------------------------------
-- Why "its own season" is the whole point: a southern-hemisphere resort skis June to
-- September. Averaging its snow cover over December to April — or over all twelve months —
-- produces a number that is not wrong so much as meaningless.
--
-- The in-season filter uses the is_month_in_season macro rather than an inline BETWEEN,
-- because 435 of 499 resorts have a season that crosses the year boundary and the naive
-- form silently matches nothing.
--
-- Fan-out: cells -> snow is one-to-many (12 months per cell), so this CTE is one row per
-- resort per month. The final aggregate collapses it back to one row per resort. The grain
-- change is deliberate and happens in one visible place.

with cells as (
    select * from {{ ref('int_resort_snow_cells') }}
),

season as (
    select * from {{ ref('int_resort_season') }}
),

resort_months as (

    select
        c.resort_id,
        s.snow_month,
        month(s.snow_month) as month_number,
        s.snow_cover_pct

    from cells c
    join {{ ref('stg_snow') }} s
      on  s.cell_latitude  = c.cell_latitude
      and s.cell_longitude = c.cell_longitude
    where c.has_snow_data

),

in_season as (

    select
        rm.*,
        {{ is_month_in_season('rm.month_number', 'se.season_start_month', 'se.season_end_month') }}
            as is_in_season

    from resort_months rm
    join season se on se.resort_id = rm.resort_id

)

select
    resort_id,

    count(*)                                              as months_observed,
    count(*) filter (where is_in_season)                  as months_in_season,

    round(avg(snow_cover_pct), 1)                         as snow_cover_pct_annual,
    round(avg(snow_cover_pct) filter (where is_in_season), 1)
                                                          as snow_cover_pct_in_season,
    round(min(snow_cover_pct) filter (where is_in_season), 1)
                                                          as snow_cover_pct_worst_month,

    -- The reliability measure that matters: how much of the season is well covered.
    round(100.0 * count(*) filter (where is_in_season and snow_cover_pct >= 50)
          / nullif(count(*) filter (where is_in_season), 0), 1)
                                                          as pct_season_months_above_50

from in_season
group by 1
