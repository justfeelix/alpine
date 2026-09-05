-- One row per resort per day. 499 x 365 = 182,135 rows.
--
-- `fct_` because this is measurements over time — many rows per resort — and the columns
-- are additive across days. The counterpart to dim_resort: join them on resort_id and the
-- join is many-to-one, which is the safe direction.
--
-- Kept at daily grain rather than pre-aggregated so that a question nobody has asked yet is
-- still answerable. The aggregates live in int_resort_weather_season; this is the source
-- they are derived from.

with weather as ( select * from {{ ref('stg_weather') }} ),
     season  as ( select * from {{ ref('int_resort_season') }} )

select
    -- Surrogate key. Not strictly required here, but it gives the grain a single handle,
    -- which matters the moment anything needs to reference a specific row.
    {{ dbt_utils.generate_surrogate_key(['w.resort_id', 'w.weather_date']) }}
        as resort_day_key,

    w.resort_id,
    w.weather_date,

    date_part('year',  w.weather_date) as weather_year,
    date_part('month', w.weather_date) as weather_month,
    date_part('doy',   w.weather_date) as day_of_year,

    -- Whether this day falls inside the resort's own season. Same macro as everywhere else.
    {{ is_month_in_season('date_part(\'month\', w.weather_date)',
                          's.season_start_month', 's.season_end_month') }} as is_in_season,

    w.temp_max_c,
    w.temp_min_c,
    w.snowfall_cm,
    w.is_freezing_day,
    w.is_snow_day,

    -- The ERA5 cell actually measured, kept so a surprising number can be traced back to
    -- the place it describes rather than the place it is labelled with.
    w.grid_latitude,
    w.grid_longitude,
    w.grid_elevation_m

from weather w
left join season s on s.resort_id = w.resort_id
