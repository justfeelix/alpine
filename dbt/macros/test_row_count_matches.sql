{#
  A generic test dbt_utils does not provide: assert that this model has exactly the same
  number of rows as another.

  This is the **fan-out canary**. A join to a table with a non-unique key silently multiplies
  rows, and every SUM downstream is then inflated — with no error, no warning, and a result
  that looks entirely plausible. The only reliable defence is to state the expected row count
  and let the build fail when it changes.

  In this project, 86 snow grid cells contain more than one resort (216 resorts affected). If
  the resort -> cell join is ever written in the wrong direction, this test is what catches it.

  Usage in a schema.yml:

      models:
        - name: dim_resort
          tests:
            - row_count_matches:
                compare_model: ref('stg_resorts')
#}

{% test row_count_matches(model, compare_model) %}

with this_model as (
    select count(*) as n from {{ model }}
),

other_model as (
    select count(*) as n from {{ compare_model }}
)

-- A test passes when it returns zero rows, so this returns a row only on disagreement.
select
    this_model.n  as actual_rows,
    other_model.n as expected_rows,
    this_model.n - other_model.n as difference
from this_model
cross join other_model
where this_model.n != other_model.n

{% endtest %}
