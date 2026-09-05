-- One row per 0.25° grid cell per month, 2022. NASA NEO MOD10C1 snow-cover percentage.
--
-- Note what this model does NOT do: it does not join to resorts. The source is a global
-- raster with no notion of a resort, and snapping coordinates to cells is business logic,
-- so it belongs in the intermediate layer (int_resort_snow_cells).

with source as (

    select * from {{ source('raw', 'snow') }}

),

renamed as (

    select
        "Month"      as snow_month,
        "Latitude"   as cell_latitude,
        "Longitude"  as cell_longitude,

        -- Percentage of the cell covered by snow, 0-100. Verified in range during profiling
        -- (observed 0.39 to 100.0), and asserted again as a test rather than trusted.
        "Snow"       as snow_cover_pct

    from source

)

select * from renamed
