-- One row per resort per day, 2022. Fetched from the Open-Meteo ERA5 archive.
--
-- The two coordinate pairs are both kept on purpose. `req_*` is what we asked for (the
-- resort's coordinates from the source file); `grid_*` is the ERA5 cell the API actually
-- served, which can be ~25 km away. Keeping both makes the offset auditable instead of
-- invisible — and `grid_elevation_m` is what exposed the geocoding failures in the source
-- data. See PROFILE.md section 7.

with source as (

    select * from {{ source('raw', 'weather') }}

),

renamed as (

    select
        resort_id,
        weather_date,

        -- what we asked for
        req_latitude        as requested_latitude,
        req_longitude       as requested_longitude,

        -- what the ERA5 grid actually gave us
        grid_latitude,
        grid_longitude,
        grid_elevation_m,
        timezone,

        temp_max_c,
        temp_min_c,
        snowfall_cm,

        -- Derived here because they are mechanical, not business decisions: a freezing day
        -- is one where the maximum stayed below zero, and a snow day is one with any
        -- measurable snowfall. Both are properties of a single row.
        temp_max_c < 0      as is_freezing_day,
        snowfall_cm > 0     as is_snow_day

    from source

)

select * from renamed
