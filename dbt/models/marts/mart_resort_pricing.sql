-- The modelling table. One row per resort, every feature the price model can use.
--
-- --------------------------------------------------------------------------------------
-- THE DECISION THIS MODEL EXISTS TO MAKE
-- --------------------------------------------------------------------------------------
-- Two groups of resorts have problems, and they need opposite treatment:
--
--   9 resorts have no price. The target is missing, so they cannot train the model — but
--   their terrain and lift data is fine, and a trained model can still *predict* for them.
--   Flagged, retained.
--
--   38 resorts have coordinates that point somewhere else entirely (PROFILE.md §7).
--   Their terrain and price are fine; it is only the *weather-derived* columns that
--   describe the wrong place. So the weather columns are set to NULL for those rows.
--
-- That second decision is the interesting one. The alternative — keeping a snow-reliability
-- number computed 2,000 km from the resort — is worse than a NULL, because a NULL announces
-- itself and a plausible wrong number does not. `coalesce` would have been the easy move
-- and the wrong one.
--
-- Nothing is dropped. `is_model_ready` says who can train, and the reason is inspectable.

with resorts     as ( select * from {{ ref('dim_resort') }} ),
     snow        as ( select * from {{ ref('int_snow_reliability') }} ),
     weather     as ( select * from {{ ref('int_resort_weather_season') }} )

select
    r.resort_id,
    r.resort_name,
    r.country,
    r.continent,
    r.hemisphere,

    -- Carried through for the map in the API layer. Kept even where the coordinates are
    -- suspect — paired with `coordinates_suspect`, so a consumer can plot them in a
    -- different colour rather than being handed a NULL and no explanation. The weather
    -- columns below are NULLed for those rows; the coordinates themselves are the evidence.
    r.latitude,
    r.longitude,

    -- 156 names lost a character to the source file's cp1252 encoding (PROFILE.md §2).
    -- Carried through so a consumer can label them rather than displaying "Uludag?-Bursa"
    -- as though it were the resort's actual name.
    r.has_encoding_damage,

    -- --------------------------------------------------------------- target
    r.price_eur,

    -- --------------------------------------------------- features: the mountain
    r.vertical_drop_m,
    r.highest_point_m,
    r.lowest_point_m,
    r.total_slopes_km,
    r.pct_difficult_terrain,
    r.pct_beginner_terrain,
    r.longest_run_km,

    -- --------------------------------------------------- features: the operation
    r.total_lifts,
    r.gondola_lifts,
    r.lift_capacity_per_hour,
    r.piste_km_per_lift,
    r.capacity_per_piste_km,
    r.snow_cannons,

    r.has_snowpark,
    r.has_night_skiing,
    r.has_summer_skiing,
    r.is_child_friendly,

    r.season_length_months,

    -- --------------------------------------------------- features: snow & weather
    -- NULLed where the coordinates are suspect. See the header: a confident number about
    -- the wrong location is more dangerous than a missing one.
    case when r.coordinates_suspect then null
         else s.snow_cover_pct_in_season end          as snow_cover_pct_in_season,
    case when r.coordinates_suspect then null
         else s.pct_season_months_above_50 end        as pct_season_months_above_50,
    case when r.coordinates_suspect then null
         else w.season_snowfall_cm end                as season_snowfall_cm,
    case when r.coordinates_suspect then null
         else w.pct_season_days_freezing end          as pct_season_days_freezing,
    case when r.coordinates_suspect then null
         else w.season_avg_temp_max_c end             as season_avg_temp_max_c,

    -- --------------------------------------------------------------- derived
    r.price_per_piste_km,

    -- --------------------------------------------------------------- eligibility
    r.has_price,
    r.coordinates_suspect,
    r.has_snow_data,
    r.has_known_season,

    -- Trainable: the model can actually use this row.
    --
    -- Defined by the features being *present*, not by proxy flags saying they should be.
    -- The first version checked has_price / not suspect / has_snow_data / has_known_season
    -- and let five rows through with a NULL snow metric — because "the resort's cell has
    -- snow data" and "that data covers the resort's season" are different claims. The
    -- singular test caught it. A flag that stands in for a condition will eventually
    -- disagree with it; asserting the condition itself cannot.
    r.price_eur is not null
        and not r.coordinates_suspect
        and s.snow_cover_pct_in_season is not null
        and w.season_snowfall_cm is not null
        and r.total_slopes_km is not null
        and r.vertical_drop_m is not null              as is_model_ready,

    -- Why a resort was excluded, in one readable column. A boolean tells you that
    -- something was dropped; this tells you what to go and fix.
    case
        when r.price_eur is null                    then 'no_price'
        when r.coordinates_suspect                  then 'suspect_coordinates'
        when not r.has_known_season                 then 'unknown_season'
        when not r.has_snow_data                    then 'no_snow_coverage'
        when s.snow_cover_pct_in_season is null     then 'no_in_season_snow_data'
        when w.season_snowfall_cm is null           then 'no_in_season_weather'
        else                                             'ok'
    end                                               as exclusion_reason

from resorts r
left join snow    s on s.resort_id = r.resort_id
left join weather w on w.resort_id = r.resort_id
