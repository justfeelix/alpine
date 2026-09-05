"""Step 4/5 — fetch daily weather per resort from the Open-Meteo archive.

This is the only part of the pipeline that talks to something outside our control, so it is
the only part that has to think about failure: the network drops, the server rate-limits us,
a request times out. Everything downstream can assume the data simply exists.

--------------------------------------------------------------------------------------
WHAT THE API ACTUALLY DOES  (verified against the live endpoint, not assumed)
--------------------------------------------------------------------------------------
1. **Multiple locations per request.** Comma-separated `latitude`/`longitude` return a JSON
   *array*, one object per location, in the order requested. 60 resorts × 122 days is one
   call returning ~260 KB. So 499 resorts is **9 requests, not 499**.

2. **The response coordinates are NOT the ones you asked for.** ERA5 is a ~0.25° grid, so
   the API snaps to the nearest cell centre and returns *that*:

       requested 60.9282, 8.3835  ->  returned 60.913883, 8.397129

   **Therefore we must not join on the returned coordinates.** The resort identity is
   carried by *array position*, and we store both coordinate pairs so the offset stays
   visible instead of becoming a silent mystery.

3. **`location_id` is missing on the first element** and present as 1, 2, 3… on the rest.
   It matches the array index, but relying on a field that is sometimes absent is a bug
   waiting to happen. We use enumeration order.

4. **`elevation` comes back per location** — the elevation of the *grid cell*. This turns
   out to be a free data-quality check: one resort in the source claims a 1300–1980 m range
   and its cell comes back at **14 m**, which means the coordinates in the source file are
   wrong. See `elevation_gap_m` in the output.

--------------------------------------------------------------------------------------
DESIGN
--------------------------------------------------------------------------------------
* **Cached to disk.** A batch already fetched is never fetched again. Re-running the
  pipeline costs nothing and doesn't hammer somebody's free service.
* **Retries only what is retryable.** 429 and 5xx and timeouts get exponential backoff;
  a 400 means our request is malformed and retrying it just wastes everyone's time.
* **Polite by default.** A small delay between live calls; the free tier is 10k/day and we
  need ~9.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import CACHE

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Three variables, not four. `precipitation_sum` was dropped deliberately: see
# `estimate_weight()` below — variables are a direct multiplier on API cost, and the
# question this project asks is about *snow*. Paying 25% of the budget for rainfall we
# would not use is a bad trade.
DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "snowfall_sum",
]

# Open-Meteo's variable name -> our column name. The parser iterates DAILY_VARS and looks
# each one up here, so adding or removing a variable changes exactly one list and the
# output follows. An earlier version hardcoded the columns in the parser; dropping
# `precipitation_sum` from the request then left the parser reading a key that no longer
# existed, and the tests did not catch it because the recorded fixture still contained the
# old variable. Deriving the columns from the request makes that class of drift impossible.
VAR_COLUMNS = {
    "temperature_2m_max": "temp_max_c",
    "temperature_2m_min": "temp_min_c",
    "snowfall_sum":       "snowfall_cm",
    "precipitation_sum":  "precipitation_mm",
    "snow_depth_max":     "snow_depth_m",
}

# 20, not 60. Open-Meteo bills by data volume, not by HTTP request — see estimate_weight().
# At 365 days x 3 variables, 20 locations costs ~162 weighted calls, comfortably under the
# 600/minute limit. 60 locations cost ~486 and left no room for anything else.
BATCH_SIZE = 20

# Rate limits on the free tier, from Open-Meteo's pricing page.
LIMIT_PER_MINUTE = 600
LIMIT_PER_HOUR = 5_000
LIMIT_PER_DAY = 10_000

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Location:
    """A resort we want weather for. `resort_id` is ours and survives the round trip."""
    resort_id: int
    latitude: float
    longitude: float


def estimate_weight(n_locations: int, n_days: int,
                    n_variables: int = len(DAILY_VARS)) -> float:
    """Estimate how many *weighted* API calls a request costs.

    Open-Meteo does not count HTTP requests — it counts data volume:

        weight ~= ceil(days / 14) * (variables / 10) * locations

    Days are charged in 14-day chunks because that is how the data is stored: returning one
    day costs the server the same as returning fourteen. Variables and locations multiply.

    This matters more than it looks. The obvious optimisation — "batch 60 resorts into one
    request instead of 60 requests" — reduces HTTP overhead but **does not reduce cost at
    all**, because the same volume is being asked for either way. Batching only controls how
    much you spend per minute.

    Worked example, and the reason this function exists: 60 locations x 365 days x 4
    variables = ceil(365/14) * 0.4 * 60 ~= 624 weighted calls, against a limit of 600 per
    minute. The first request consumed the entire minute's budget and the second was
    rejected with a 429.
    """
    import math
    return math.ceil(n_days / 14) * (n_variables / 10) * n_locations


# --------------------------------------------------------------------------- transport
def _cache_key(lats, lons, start, end) -> Path:
    """Cache on the *request*, so changing dates or coordinates busts it automatically."""
    payload = json.dumps([lats, lons, start, end, DAILY_VARS], sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return CACHE / f"openmeteo_{digest}.json"


def fetch_batch(locations: list[Location], start: str, end: str, *,
                client: httpx.Client | None = None,
                use_cache: bool = True,
                max_retries: int = 4,
                pause_s: float = 8.0) -> list[dict]:
    """Fetch one batch of locations. Returns the raw JSON array, unmodified."""
    lats = ",".join(f"{loc.latitude:.4f}" for loc in locations)
    lons = ",".join(f"{loc.longitude:.4f}" for loc in locations)

    cache_path = _cache_key(lats, lons, start, end)
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())

    params = {
        "latitude": lats,
        "longitude": lons,
        "start_date": start,
        "end_date": end,
        "daily": ",".join(DAILY_VARS),
        "timezone": "auto",
    }

    owned = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                r = client.get(ARCHIVE_URL, params=params)

                if r.status_code in RETRYABLE_STATUS:
                    # 429 is a *rate* limit, and Open-Meteo's tightest window is per minute.
                    # Backing off 1-2-4-8 seconds — which is right for a flaky connection —
                    # is useless here: it exhausts the retries inside the same minute that is
                    # already over quota. Wait out the window instead.
                    if r.status_code == 429:
                        wait = float(r.headers.get("Retry-After", 65))
                    else:
                        wait = float(r.headers.get("Retry-After", 2 ** attempt))

                    detail = ""
                    try:
                        detail = r.json().get("reason", "")
                    except Exception:  # noqa: BLE001
                        pass
                    print(f"    HTTP {r.status_code} {detail} - waiting {wait:.0f}s "
                          f"(attempt {attempt + 1}/{max_retries})", flush=True)

                    last_error = httpx.HTTPStatusError(
                        f"HTTP {r.status_code} {detail}".strip(),
                        request=r.request, response=r)
                    time.sleep(wait)
                    continue

                if r.status_code >= 400:
                    # Our fault. Retrying an malformed request is pointless — fail loudly,
                    # and include the server's explanation, which Open-Meteo does provide.
                    try:
                        reason = r.json().get("reason", r.text[:200])
                    except Exception:  # noqa: BLE001
                        reason = r.text[:200]
                    raise ValueError(f"Open-Meteo rejected the request "
                                     f"(HTTP {r.status_code}): {reason}")

                data = r.json()
                # A single location returns an object, several return an array. Normalise.
                if isinstance(data, dict):
                    data = [data]

                if use_cache:
                    CACHE.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(data))
                time.sleep(pause_s)
                return data

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                time.sleep(2 ** attempt)

        raise RuntimeError(
            f"Open-Meteo failed after {max_retries} attempts: {last_error}\n"
            f"Completed batches are cached, so re-running resumes rather than restarting. "
            f"If this keeps happening, lower BATCH_SIZE or shorten the date range — "
            f"see estimate_weight()."
        ) from last_error
    finally:
        if owned:
            client.close()


# ----------------------------------------------------------------------------- parsing
def parse_batch(payload: list[dict], locations: list[Location]) -> list[dict]:
    """Flatten the API's column-oriented JSON into one row per resort per day.

    Pairs each response object with its request by **array position** — see the module
    docstring for why the returned coordinates cannot be used as a key.
    """
    if len(payload) != len(locations):
        raise ValueError(
            f"Open-Meteo returned {len(payload)} locations for {len(locations)} requested. "
            "Row alignment cannot be trusted; refusing to guess.")

    rows: list[dict] = []
    for loc, obj in zip(locations, payload):
        daily = obj["daily"]

        missing = [v for v in DAILY_VARS if v not in daily]
        if missing:
            raise ValueError(
                f"Response is missing requested variables: {missing}. "
                f"Got {sorted(k for k in daily if k != 'time')}. "
                "The request and the parser have diverged.")

        for i, day in enumerate(daily["time"]):
            row = {
                "resort_id": loc.resort_id,
                "weather_date": day,
                # what we asked for
                "req_latitude": loc.latitude,
                "req_longitude": loc.longitude,
                # what the grid actually gave us — keep both, so the offset is auditable
                "grid_latitude": obj["latitude"],
                "grid_longitude": obj["longitude"],
                "grid_elevation_m": obj.get("elevation"),
                "timezone": obj.get("timezone"),
            }
            for var in DAILY_VARS:
                row[VAR_COLUMNS[var]] = daily[var][i]
            rows.append(row)
    return rows


def fetch_all(locations: list[Location], start: str, end: str, *,
              batch_size: int = BATCH_SIZE, progress: bool = True,
              **kwargs) -> list[dict]:
    """Fetch every location, in batches. Returns one row per resort per day."""
    rows: list[dict] = []
    batches = [locations[i:i + batch_size] for i in range(0, len(locations), batch_size)]

    with httpx.Client(timeout=60.0) as client:
        for n, batch in enumerate(batches, start=1):
            if progress:
                print(f"  batch {n}/{len(batches)}  ({len(batch)} resorts)", flush=True)
            payload = fetch_batch(batch, start, end, client=client, **kwargs)
            rows.extend(parse_batch(payload, batch))
    return rows


# ------------------------------------------------------------------------------ load
# snow.csv covers calendar 2022, so we fetch the same year. Comparing 2022 snow cover with
# 2022 weather is a decision, not an accident — mismatched periods would make any
# correlation between them meaningless.
DEFAULT_START = "2022-01-01"
DEFAULT_END = "2022-12-31"


def locations_from_warehouse(con) -> list[Location]:
    """Every resort, as a location to fetch. The source of truth is the warehouse."""
    rows = con.execute("""
        SELECT "ID", "Latitude", "Longitude"
        FROM raw.resorts
        WHERE "Latitude" BETWEEN -90 AND 90
          AND "Longitude" BETWEEN -180 AND 180
        ORDER BY "ID"
    """).fetchall()
    return [Location(resort_id=int(r[0]), latitude=float(r[1]), longitude=float(r[2]))
            for r in rows]


def load_weather(con=None, start: str = DEFAULT_START, end: str = DEFAULT_END,
                 **kwargs) -> int:
    """Fetch weather for every resort and land it in `raw.weather`. Idempotent."""
    from datetime import date

    import pandas as pd

    from .seed import connect

    owned = con is None
    con = con or connect()
    try:
        locations = locations_from_warehouse(con)

        n_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        n_batches = -(-len(locations) // BATCH_SIZE)
        per_call = estimate_weight(BATCH_SIZE, n_days)
        total = estimate_weight(len(locations), n_days)

        print(f"Fetching {start} to {end} ({n_days} days) for {len(locations)} resorts")
        print(f"  {n_batches} requests of {BATCH_SIZE} locations, "
              f"{len(DAILY_VARS)} variables")
        print(f"  estimated cost: {per_call:.0f} weighted calls each, "
              f"{total:.0f} total")
        print(f"  free-tier limits: {LIMIT_PER_MINUTE}/min, {LIMIT_PER_HOUR}/hour, "
              f"{LIMIT_PER_DAY}/day")
        if per_call >= LIMIT_PER_MINUTE:
            raise ValueError(
                f"A single request would cost {per_call:.0f} weighted calls against a "
                f"{LIMIT_PER_MINUTE}/min limit. Lower BATCH_SIZE or shorten the range.")
        if total >= LIMIT_PER_HOUR:
            print(f"  WARNING: total exceeds the hourly limit; expect to wait it out.")
        print()

        rows = fetch_all(locations, start, end, **kwargs)
        df = pd.DataFrame(rows)
        df["weather_date"] = pd.to_datetime(df["weather_date"]).dt.date

        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.register("_weather", df)
        con.execute("CREATE OR REPLACE TABLE raw.weather AS SELECT * FROM _weather")
        con.unregister("_weather")

        return con.execute("SELECT count(*) FROM raw.weather").fetchone()[0]
    finally:
        if owned:
            con.close()
