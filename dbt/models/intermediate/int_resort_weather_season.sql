-- Daily weather aggregated to one row per resort, in-season only.
--
-- Grain: one row per resort (only resorts with a known season and weather data).
--
-- Same in-season logic as int_snow_reliability, via the same macro — which is the reason
-- the macro exists. Two models computing "in season" independently is how they start to
-- disagree.

with weather as (
    select * from {{ ref('stg_weather') }}
),

season as (
    select * from {{ ref('int_resort_season') }}
),

joined as (

    select
        w.*,
        month(w.weather_date) as month_number,
        {{ is_month_in_season('month(w.weather_date)',
                              'se.season_start_month', 'se.season_end_month') }}
            as is_in_season
    from weather w
    join season se on se.resort_id = w.resort_id

)

select
    resort_id,

    count(*) filter (where is_in_season)                        as season_days,

    round(sum(snowfall_cm) filter (where is_in_season), 1)      as season_snowfall_cm,
    round(avg(temp_max_c)  filter (where is_in_season), 1)      as season_avg_temp_max_c,
    round(avg(temp_min_c)  filter (where is_in_season), 1)      as season_avg_temp_min_c,

    count(*) filter (where is_in_season and is_snow_day)        as season_snow_days,
    count(*) filter (where is_in_season and is_freezing_day)    as season_freezing_days,

    -- Share of season days that stayed below freezing all day. A direct proxy for whether
    -- the snow that falls actually stays, which is what "snow reliable" means in practice.
    round(100.0 * count(*) filter (where is_in_season and is_freezing_day)
          / nullif(count(*) filter (where is_in_season), 0), 1) as pct_season_days_freezing

from joined
group by 1
