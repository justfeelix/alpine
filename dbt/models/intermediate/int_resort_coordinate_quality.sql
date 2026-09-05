-- Flag resorts whose coordinates in the source are implausible.
--
-- Grain: one row per resort.
--
-- --------------------------------------------------------------------------------------
-- Cross-validation against an independent source. See PROFILE.md section 7.
--
-- The Kaggle coordinates pass every check that can be run against the file itself: correct
-- type, in range, no nulls, no duplicates. 38 of them are still wrong — Arapahoe Basin is
-- listed 2,144 km from the actual resort, in West Virginia rather than Colorado. These are
-- geocoding failures from name collisions (there is a Keystone in Iowa, an Alta in Iowa).
--
-- The tell only appears when a second source is consulted. Open-Meteo returns the elevation
-- of the ERA5 grid cell it served, so comparing that against the resort's own stated base
-- elevation gives an independent opinion on whether the coordinates point at a mountain.
--
--   negative gap  ->  normal. ERA5 smooths terrain over ~25 km, so a cell containing an
--                     alpine valley averages ABOVE the valley floor. Switzerland's mean gap
--                     is -678 m, which is the physics working, not an error.
--   large positive -> anomaly. A resort claiming a base 500 m above its own grid cell is
--                     claiming to sit on a mountain the terrain model says is not there.
--
-- The threshold is a heuristic, validated against five resorts whose real locations are
-- independently verifiable. Flagged, never silently dropped.

with resorts as (
    select resort_id, lowest_point_m, highest_point_m from {{ ref('stg_resorts') }}
),

-- One row per resort. grid_elevation_m is constant across a resort's 365 days, but taking
-- min() rather than assuming that is cheaper than being wrong.
grid as (
    select resort_id, min(grid_elevation_m) as grid_elevation_m
    from {{ ref('stg_weather') }}
    group by 1
)

select
    r.resort_id,
    r.lowest_point_m,
    g.grid_elevation_m,
    r.lowest_point_m - g.grid_elevation_m as elevation_gap_m,

    case
        when g.grid_elevation_m is null                     then 'no_weather_data'
        when r.lowest_point_m - g.grid_elevation_m > 1000    then 'almost_certainly_wrong'
        when r.lowest_point_m - g.grid_elevation_m >  500    then 'suspect'
        else                                                      'plausible'
    end as coordinate_quality,

    coalesce(r.lowest_point_m - g.grid_elevation_m, 0) > {{ var('elevation_gap_threshold_m') }}
        as coordinates_suspect

from resorts r
left join grid g on g.resort_id = r.resort_id
