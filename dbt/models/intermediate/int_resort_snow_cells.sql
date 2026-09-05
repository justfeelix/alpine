-- Snap each resort to its NASA NEO snow grid cell.
--
-- Grain: **one row per resort** — identical to stg_resorts. `row_count_matches` asserts it.
--
-- --------------------------------------------------------------------------------------
-- THE SPATIAL JOIN, AND WHY IT IS DANGEROUS
-- --------------------------------------------------------------------------------------
-- snow.csv is a global raster: one row per 0.25° cell per month, with no notion of a
-- resort. To use it we compute which cell each resort falls in:
--
--     cell_lat = floor(lat / 0.25) * 0.25 + 0.125
--
-- Grid centres sit at .125/.375/.625/.875 — verified against the raw file (zero cells
-- off-grid) rather than taken from the documentation.
--
-- **Direction matters.** A resort falls in exactly one cell, so resort -> cell is
-- many-to-one and the row count cannot grow. The reverse is not safe: 86 cells contain more
-- than one resort (216 resorts affected), because at 0.25° a cell is ~25 km and
-- neighbouring resorts share a valley. Joining cell -> resort would fan out.
--
-- The defence is structural, not hopeful: `cell_coverage` is aggregated to one row per cell
-- **before** the join, so the right-hand side is unique by construction. A `left join` onto
-- a unique key cannot multiply rows.
--
-- 22 resorts land on a cell absent from the NEO product — masked as water, most likely.
-- They are kept with `has_snow_data = false` rather than dropped: silently losing 4% of the
-- population biases everything computed afterwards and leaves no trace of why.

with resorts as (

    select * from {{ ref('stg_resorts') }}

),

snapped as (

    select
        resort_id,
        latitude,
        longitude,
        floor(latitude  / {{ var('snow_grid_deg') }}) * {{ var('snow_grid_deg') }}
            + {{ var('snow_grid_offset') }} as cell_latitude,
        floor(longitude / {{ var('snow_grid_deg') }}) * {{ var('snow_grid_deg') }}
            + {{ var('snow_grid_offset') }} as cell_longitude

    from resorts

),

-- One row per cell. Aggregating first is what makes the join safe.
cell_coverage as (

    select
        cell_latitude,
        cell_longitude,
        count(*) as snow_months_observed

    from {{ ref('stg_snow') }}
    group by 1, 2

)

select
    s.resort_id,
    s.latitude,
    s.longitude,
    s.cell_latitude,
    s.cell_longitude,

    coalesce(c.snow_months_observed, 0) as snow_months_observed,
    c.cell_latitude is not null         as has_snow_data

from snapped s
left join cell_coverage c
    on  s.cell_latitude  = c.cell_latitude
    and s.cell_longitude = c.cell_longitude
