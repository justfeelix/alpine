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

DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "snowfall_sum",
    "precipitation_sum",
]

# 60 fits comfortably in a URL and returns ~260 KB. Larger batches risk hitting URL length
# limits at some proxy we don't control, for no real gain — 9 calls is already trivial.
BATCH_SIZE = 60

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Location:
    """A resort we want weather for. `resort_id` is ours and survives the round trip."""
    resort_id: int
    latitude: float
    longitude: float


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
                pause_s: float = 1.0) -> list[dict]:
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
                    # Honour Retry-After when the server tells us; otherwise back off.
                    wait = float(r.headers.get("Retry-After", 2 ** attempt))
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {r.status_code}", request=r.request, response=r)
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
            f"Open-Meteo failed after {max_retries} attempts: {last_error}") from last_error
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
        times = daily["time"]
        for i, day in enumerate(times):
            rows.append({
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
                "temp_max_c": daily["temperature_2m_max"][i],
                "temp_min_c": daily["temperature_2m_min"][i],
                "snowfall_cm": daily["snowfall_sum"][i],
                "precipitation_mm": daily["precipitation_sum"][i],
            })
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
    import pandas as pd

    from .seed import connect

    owned = con is None
    con = con or connect()
    try:
        locations = locations_from_warehouse(con)
        print(f"Fetching {start} to {end} for {len(locations)} resorts "
              f"({-(-len(locations) // BATCH_SIZE)} requests)")

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
