-- One row per resort. Casting, renaming, and the missing-value decisions from PROFILE.md.
--
-- Staging is deliberately boring: no joins, no business logic, no aggregation. Its whole
-- job is that everything downstream can assume clean types and honest NULLs, and that a
-- source column name appears in exactly one place in the project.
--
-- The interesting content here is *which zeros become NULL*, and why each one differs.

with source as (

    select * from {{ source('raw', 'resorts') }}

),

renamed as (

    select
        "ID"                                as resort_id,
        trim("Resort")                      as resort_name,
        "Country"                           as country,
        "Continent"                         as continent,
        "Latitude"                          as latitude,
        "Longitude"                         as longitude,

        -- ------------------------------------------------------------------ the target
        -- Nine resorts are priced 0, including Perisher (Australia's largest) and the
        -- Yellowstone Club (six-figure membership). Neither is free: 0 means "unknown".
        -- Left as 0 it would teach a price model that big resorts are sometimes free.
        nullif("Price", 0)                  as price_eur,

        -- --------------------------------------------------------------------- terrain
        "Highest point"                     as highest_point_m,
        "Lowest point"                      as lowest_point_m,
        "Highest point" - "Lowest point"    as vertical_drop_m,

        "Beginner slopes"                   as beginner_slopes_km,
        "Intermediate slopes"               as intermediate_slopes_km,
        "Difficult slopes"                  as difficult_slopes_km,
        "Total slopes"                      as total_slopes_km,

        -- Every resort has a longest run, so 0 is missing, not a measurement.
        nullif("Longest run", 0)            as longest_run_km,

        -- ----------------------------------------------------------------- lifts & snow
        "Surface lifts"                     as surface_lifts,
        "Chair lifts"                       as chair_lifts,
        "Gondola lifts"                     as gondola_lifts,

        -- One resort reports 80 km of piste served by 0 lifts. That is missing data.
        nullif("Total lifts", 0)            as total_lifts,
        nullif("Lift capacity", 0)          as lift_capacity_per_hour,

        -- NOT nullif'd, deliberately. 226 resorts (45%) report zero snow cannons, and for
        -- a small resort that is a genuine measurement rather than an absent one. Treating
        -- it as NULL would throw away a real signal about how much a resort invests in
        -- snow-making — which is directly relevant to a question about snow and price.
        "Snow cannons"                      as snow_cannons,

        -- ------------------------------------------------------------------- attributes
        "Child friendly" = 'Yes'            as is_child_friendly,
        "Snowparks"      = 'Yes'            as has_snowpark,
        "Nightskiing"    = 'Yes'            as has_night_skiing,
        "Summer skiing"  = 'Yes'            as has_summer_skiing,

        -- Parsed properly in int_resort_season; kept raw here so staging stays mechanical.
        "Season"                            as season_raw,

        -- ---------------------------------------------------------------- data quality
        -- 156 rows contain a literal '?' where a character was destroyed before the file
        -- was ever written. Unrecoverable, so flagged rather than "fixed" — but split into
        -- two kinds, because they have different consequences:
        --
        --   destroyed letter    (Chilla?n, Val d'Ise?re)  -> the name is misspelt, so it
        --                                                    will not match any other source
        --   destroyed separator (La Rosière/?La Thuile)   -> cosmetic only
        --
        -- Only the first kind blocks joining this resort to an external dataset by name.
        "Resort" like '%?%'                                   as has_encoding_damage,
        regexp_matches("Resort", '[a-zA-Z]\?[a-zA-Z]')       as name_letter_destroyed,

        case when "Latitude" < 0 then 'southern' else 'northern' end as hemisphere

    from source

)

select * from renamed
