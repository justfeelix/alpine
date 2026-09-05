{#
  Is a given month inside a resort's season, accounting for the year boundary?

  THE WHOLE POINT. For a season of December (12) to April (4):

      month between 12 and 4        -> matches nothing, silently
      wrap-aware version below      -> matches Dec, Jan, Feb, Mar, Apr

  405 of 499 resorts have a wrapping season, so getting this wrong discards 81% of the
  data and returns an answer that looks entirely reasonable.

  Kept as a macro so the condition exists once. Every model that filters to in-season
  months calls this rather than writing the predicate again — which is exactly the kind of
  duplication that lets two models quietly disagree.
#}
{% macro is_month_in_season(month_expr, start_expr, end_expr) %}
    (
        {{ start_expr }} is not null
        and (
            -- normal range: June - October
            ({{ start_expr }} <= {{ end_expr }}
             and {{ month_expr }} between {{ start_expr }} and {{ end_expr }})

            -- wrapping range: December - April
            or ({{ start_expr }} > {{ end_expr }}
                and ({{ month_expr }} >= {{ start_expr }}
                  or {{ month_expr }} <= {{ end_expr }}))
        )
    )
{% endmacro %}
