#!/usr/bin/env bash
# End-to-end verification of the Alpine service.
#
# ------------------------------------------------------------------------------------
# WHY THIS EXISTS, AND WHY IT IS NOT A UNIT TEST
# ------------------------------------------------------------------------------------
# The unit tests in tests/ check the extract layer against a recorded fixture. dbt's 90
# data tests check the warehouse. Neither of them can tell you that the *service* works,
# because the failure modes here are the seams: a model pickled by a different scikit-learn,
# a mart that was never built, a column renamed upstream that the API still selects by name.
#
# Three habits this script has, all of them learned the hard way:
#
#   1. **Prerequisites are checked before the server starts.** A missing model produces one
#      clear line, not eight cascading 503s that you then have to trace back.
#
#   2. **Failures print the status code AND the response body.** `curl -sf` swallows the
#      body on a non-2xx, which reduces every server-side error to "exit code 22" — the
#      least useful diagnostic available. FastAPI puts the real reason in the body.
#
#   3. **The port is discovered, not assumed.** Hardcoding 8000 means the suite fails on any
#      machine already running something there, and reports it as a broken service.

set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
green() { printf '\033[32m  PASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
red()   { printf '\033[31m  FAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
info()  { printf '\033[2m        %s\033[0m\n' "$1"; }

# ------------------------------------------------------------------- prerequisites
echo
echo "=== Prerequisites ==="

WAREHOUSE="${ALPINE_WAREHOUSE:-data/warehouse.duckdb}"
[[ -f "$WAREHOUSE" ]] \
  && green "warehouse exists ($WAREHOUSE)" \
  || { red "warehouse missing — run 'make seed && make weather'"; exit 1; }

python -c "
import duckdb, sys
con = duckdb.connect('$WAREHOUSE', read_only=True)
n = con.execute(\"select count(*) from duckdb_tables() where table_name='mart_resort_pricing'\").fetchone()[0]
sys.exit(0 if n else 1)
" 2>/dev/null \
  && green "mart_resort_pricing built" \
  || { red "marts missing — run 'cd dbt && dbt build'"; exit 1; }

[[ -f models/metrics.json ]] \
  && green "models/metrics.json present" \
  || { red "metrics missing — run 'make model'"; exit 1; }

# Actually LOAD the pickle rather than checking the file exists. This is the exact check
# that would have caught the Cadence failure where /health and /model passed while every
# endpoint that touched the model returned 500: the file was there, it just would not load
# under the installed scikit-learn.
python -c "
import joblib, sklearn, sys
try:
    m = joblib.load('models/pricing_model.joblib')
except Exception as e:
    print(f'        {type(e).__name__}: {e}'); sys.exit(1)
if m['sklearn_version'] != sklearn.__version__:
    print(f\"        pickled with sklearn {m['sklearn_version']}, running {sklearn.__version__}\")
print(f\"        {len(m['features'])} features, {m['n_training_rows']} training rows\")
" \
  && green "model loads under the installed scikit-learn" \
  || { red "model will not load — run 'make model'"; exit 1; }

# ----------------------------------------------------------------------- start server
PORT=""
for p in 8000 8001 8002 8003 8004; do
  if ! (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null; then PORT=$p; break; fi
done
[[ -n "$PORT" ]] || { red "no free port in 8000-8004"; exit 1; }

echo
echo "=== Starting service on port $PORT ==="
python -m uvicorn alpine.serve:app --port "$PORT" --log-level warning >/tmp/alpine-serve.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null' EXIT

BASE="http://127.0.0.1:$PORT"
for _ in $(seq 1 40); do
  curl -s -o /dev/null "$BASE/health" && break
  sleep 0.5
done
kill -0 $SERVER_PID 2>/dev/null \
  || { red "server died on startup:"; cat /tmp/alpine-serve.log; exit 1; }

# --------------------------------------------------------------------------- helpers
# Fetch once, capture body and status separately, and hand the caller both. Every check
# below therefore has the real error text available when it fails.
BODY=""; CODE=""
fetch() {  # fetch METHOD PATH [JSON]
  local out
  if [[ "$1" == "POST" ]]; then
    out=$(curl -s -w '\n%{http_code}' -X POST -H 'Content-Type: application/json' \
          -d "${3:-{\}}" "$BASE$2")
  else
    out=$(curl -s -w '\n%{http_code}' "$BASE$2")
  fi
  CODE="${out##*$'\n'}"; BODY="${out%$'\n'*}"
}

# check NAME EXPECTED_CODE JQ_EXPR   — passes when the status matches and jq returns true
check() {
  if [[ "$CODE" != "$2" ]]; then
    red "$1"; info "expected HTTP $2, got $CODE"; info "${BODY:0:400}"; return
  fi
  if [[ -n "${3:-}" ]] && ! echo "$BODY" | python -c "
import json,sys
d = json.load(sys.stdin)
sys.exit(0 if ($3) else 1)
" 2>/dev/null; then
    red "$1"; info "assertion failed: $3"; info "${BODY:0:400}"; return
  fi
  green "$1"
}

echo
echo "=== Endpoints ==="

fetch GET /health
check "GET /health -> 200, model loaded" 200 "d['model_loaded'] and d['metrics_loaded']"

fetch GET "/resorts?limit=5"
check "GET /resorts -> 5 rows, 499 total" 200 "len(d['resorts'])==5 and d['total']==499"

fetch GET "/resorts?country=Austria&limit=200"
check "GET /resorts?country=Austria -> all Austrian" 200 \
      "d['total']>0 and all(r['country']=='Austria' for r in d['resorts'])"

fetch GET "/resorts?model_ready_only=true&limit=1"
check "GET /resorts?model_ready_only -> 422 (matches the model)" 200 "d['total']==422"

fetch GET "/resorts?min_snow=90&limit=100"
check "GET /resorts?min_snow=90 -> filter applied" 200 \
      "all(r['snow_cover_pct_in_season']>=90 for r in d['resorts'])"

# Pagination must not overlap: page 2 has to be disjoint from page 1, which is the bug you
# get from ordering on a non-unique column without a tiebreaker.
fetch GET "/resorts?limit=10&offset=0"; P1="$BODY"
fetch GET "/resorts?limit=10&offset=10"
check "GET /resorts pagination -> pages are disjoint" 200 \
      "not ({r['resort_id'] for r in d['resorts']} & {r['resort_id'] for r in json.loads('''$P1''')['resorts']})"

fetch GET /resorts/1
check "GET /resorts/1 -> has a prediction and a residual" 200 \
      "'predicted_price_eur' in d and d['residual_eur'] is not None"

fetch GET /resorts/999999
check "GET /resorts/999999 -> 404" 404 ""

fetch GET /countries
check "GET /countries -> Austria present, snow + price side by side" 200 \
      "any(c['country']=='Austria' for c in d['countries']) and \
       all('avg_snow_cover_pct' in c and 'avg_price_eur' in c for c in d['countries'])"

fetch GET /model
check "GET /model -> lift over baseline is positive" 200 \
      "d['headline']['lift_over_baseline_eur']>0"

check "GET /model -> snow verdict is 'not significant'" 200 \
      "d['snow_verdict']['significant'] is False"

fetch POST /predict '{"country":"Austria","total_slopes_km":120,"vertical_drop_m":1200}'
check "POST /predict -> plausible price with an error bar" 200 \
      "10 < d['predicted_price_eur'] < 200 and d['expected_error_eur'] > 0"

# Country is the one required field. A request without it must be rejected by the schema
# before it ever reaches the model — 422 is FastAPI/Pydantic doing validation, not a crash.
fetch POST /predict '{"total_slopes_km":120}'
check "POST /predict without country -> 422 from validation" 422 ""

# The imputer means a nearly-empty request still works. Worth asserting, because it is a
# deliberate property of the pipeline rather than an accident.
fetch POST /predict '{"country":"Austria"}'
check "POST /predict with country only -> imputer fills the rest" 200 \
      "d['predicted_price_eur'] > 0"

fetch GET /predictions/missing-price
check "GET /predictions/missing-price -> the 9 retained rows are priced" 200 \
      "d['n']==9 and all(p['predicted_price_eur']>0 for p in d['predictions'])"

# ------------------------------------------------------------------ cross-check
# The claim the whole project rests on, asserted rather than trusted: country separates
# prices far more than snow does. If a refactor ever inverted a join, this fails.
fetch GET /countries
check "cross-check: price spread across countries exceeds EUR 20" 200 \
      "max(c['avg_price_eur'] for c in d['countries']) - \
       min(c['avg_price_eur'] for c in d['countries']) > 20"

echo
echo "==============================================="
printf "  %d passed, %d failed\n" "$PASS" "$FAIL"
echo "==============================================="
exit $(( FAIL > 0 ))
