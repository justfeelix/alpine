{#
  Month name -> month number.

  A macro rather than a repeated CASE, because it appears four times in
  int_resort_season alone. If the source ever starts abbreviating ("Dec" rather than
  "December") there is one place to change.

  Returns NULL for anything unrecognised, deliberately: an unexpected month name should
  surface as a NULL that a not_null test catches, not as a silent 1.
#}
{% macro month_name_to_number(expr) %}
    case {{ expr }}
        when 'January'   then 1
        when 'February'  then 2
        when 'March'     then 3
        when 'April'     then 4
        when 'May'       then 5
        when 'June'      then 6
        when 'July'      then 7
        when 'August'    then 8
        when 'September' then 9
        when 'October'   then 10
        when 'November'  then 11
        when 'December'  then 12
        else null
    end
{% endmacro %}
