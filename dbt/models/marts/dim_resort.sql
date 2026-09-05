-- One row per resort. Everything descriptive, in one place.
--
-- `dim_` because the grain is one row per *thing*, and the columns describe it rather than
-- measure events. That naming is not decoration: it tells the next person whether SUM is a
-- sensible thing to do before they open the file.
--
-- All 499 resorts are here, including the ones with problems. Quality flags travel with the
-- row so a consumer can decide; nothing is silently withheld.

with resorts       as ( select * from {{ ref('stg_resorts') }} ),
     season        as ( select * from {{ ref('int_resort_season') }} ),
     coord_quality as ( select * from {{ ref('int_resort_coordinate_quality') }} ),
     snow_cells    as ( select * from {{ ref('int_resort_snow_cells') }} )

select
    r.resort_id,
    r.resort_name,
    r.country,
    r.continent,
    r.hemisphere,
    r.latitude,
    r.longitude,

    -- ------------------------------------------------------------------------ pricing
    r.price_eur,

    -- Value per kilometre of piste. NULLIF guards the denominator as a reflex: one resort
    -- with zero slopes would otherwise take the whole model down at 3am.
    round(r.price_eur / nullif(r.total_slopes_km, 0), 2) as price_per_piste_km,

    -- --------------------------------------------------------------------- the mountain
    r.highest_point_m,
    r.lowest_point_m,
    r.vertical_drop_m,
    r.total_slopes_km,
    r.beginner_slopes_km,
    r.intermediate_slopes_km,
    r.difficult_slopes_km,
    r.longest_run_km,

    round(100.0 * r.difficult_slopes_km / nullif(r.total_slopes_km, 0), 1)
        as pct_difficult_terrain,
    round(100.0 * r.beginner_slopes_km / nullif(r.total_slopes_km, 0), 1)
        as pct_beginner_terrain,

    -- ---------------------------------------------------------------------- the lifts
    r.total_lifts,
    r.surface_lifts,
    r.chair_lifts,
    r.gondola_lifts,
    r.lift_capacity_per_hour,
    r.snow_cannons,

    round(r.total_slopes_km / nullif(r.total_lifts, 0), 2)     as piste_km_per_lift,
    round(r.lift_capacity_per_hour / nullif(r.total_slopes_km, 0))
                                                               as capacity_per_piste_km,

    -- ------------------------------------------------------------------------ features
    r.is_child_friendly,
    r.has_snowpark,
    r.has_night_skiing,
    r.has_summer_skiing,

    -- -------------------------------------------------------------------------- season
    s.season_raw,
    s.season_format,
    s.season_start_month,
    s.season_end_month,
    s.season_length_months,
    s.season_wraps_year,
    s.has_known_season,

    -- ------------------------------------------------------------------- data quality
    -- Carried on the dimension so that any consumer can see the caveats without having to
    -- know they exist. A flag nobody can find is not a flag.
    cq.grid_elevation_m,
    cq.elevation_gap_m,
    cq.coordinate_quality,
    cq.coordinates_suspect,
    sc.has_snow_data,
    r.has_encoding_damage,
    r.name_letter_destroyed,

    r.price_eur is not null as has_price

from resorts r
left join season        s  on s.resort_id  = r.resort_id
left join coord_quality cq on cq.resort_id = r.resort_id
left join snow_cells    sc on sc.resort_id = r.resort_id
