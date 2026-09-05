{#
  Assert a column has more than one distinct value.

  Catches a specific and nasty failure: a transformation that silently collapses a column to
  a single value — a bad CASE that always falls through to ELSE, a join that nulls everything,
  a filter applied one layer too early. `not_null` passes, `accepted_values` passes, and the
  column is still useless.
#}

{% test not_constant(model, column_name) %}

select
    count(distinct {{ column_name }}) as distinct_values
from {{ model }}
having count(distinct {{ column_name }}) <= 1

{% endtest %}
