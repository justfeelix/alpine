-- Parse the free-text `Season` column into start and end months.
--
-- Grain: one row per resort. Unchanged from stg_resorts.
--
-- --------------------------------------------------------------------------------------
-- WHY THIS NEEDS A MODEL OF ITS OWN
-- --------------------------------------------------------------------------------------
-- The source stores the season as a human-readable string, and profiling found four
-- different shapes, not one:
--
--     "December - April"   220 rows   a range, and it WRAPS the year boundary
--     "June - October"      10 rows   a range that does not wrap (southern hemisphere)
--     "April"                5 rows   a single month, no dash
--     "Year-round"           4 rows   not a range at all
--     "Unknown"             27 rows   no data
--
-- A naive `split_part(season, ' - ', 1)` handles the first two and quietly produces
-- nonsense for the rest.
--
-- --------------------------------------------------------------------------------------
-- THE TRAP THAT MAKES THIS INTERESTING
-- --------------------------------------------------------------------------------------
-- For "December - April", start_month = 12 and end_month = 4. The obvious predicate
--
--     where month between start_month and end_month     -- 12 to 4
--
-- matches **nothing**. Not an error, not a warning — an empty result. 405 of 499 resorts
-- have a season that wraps, so a naive BETWEEN silently discards 81% of the data and
-- returns a perfectly plausible-looking answer built on the remaining 19%.
--
-- The wrap-aware form is `is_month_in_season` in macros/. Every downstream model uses it
-- rather than reimplementing the condition, so the logic exists in exactly one place.

with resorts as (

    select * from {{ ref('stg_resorts') }}

),

classified as (

    select
        resort_id,
        hemisphere,
        season_raw,

        case
            when season_raw = 'Unknown'                then 'unknown'
            when season_raw = 'Year-round'             then 'year_round'
            when season_raw like '% - %'               then 'range'
            else                                            'single_month'
        end as season_format

    from resorts

),

parsed as (

    select
        resort_id,
        hemisphere,
        season_raw,
        season_format,

        case season_format
            when 'year_round' then 1
            when 'unknown'    then null
            when 'range'      then {{ month_name_to_number("trim(split_part(season_raw, ' - ', 1))") }}
            else                   {{ month_name_to_number("trim(season_raw)") }}
        end as season_start_month,

        case season_format
            when 'year_round' then 12
            when 'unknown'    then null
            when 'range'      then {{ month_name_to_number("trim(split_part(season_raw, ' - ', 2))") }}
            else                   {{ month_name_to_number("trim(season_raw)") }}
        end as season_end_month

    from classified

)

select
    resort_id,
    hemisphere,
    season_raw,
    season_format,
    season_start_month,
    season_end_month,

    -- Does this season cross the new year? Kept as a column because it is the thing that
    -- makes every downstream predicate non-obvious, and naming it makes that visible.
    season_start_month > season_end_month as season_wraps_year,

    -- Length in months, wrap-aware. December-April is 5 months, not -8.
    case
        when season_start_month is null then null
        when season_start_month <= season_end_month
            then season_end_month - season_start_month + 1
        else 12 - season_start_month + season_end_month + 1
    end as season_length_months,

    season_start_month is not null as has_known_season

from parsed
