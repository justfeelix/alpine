"""Tests for the Open-Meteo client.

The fixture is a **real captured response** from the live API — 3 resorts, 4 days, including
the awkward bits: a missing `location_id` on the first element, snapped coordinates, and a
resort whose grid elevation is obviously wrong for a ski resort.

Testing the parser against a recorded response means the parsing logic is genuinely verified
without needing the network in CI. The transport layer (retries, caching) is tested against a
mock, for the same reason.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from alpine.weather import Location, fetch_batch, parse_batch

FIXTURE = Path(__file__).parent / "fixtures" / "openmeteo_3_locations.json"

# The coordinates we *asked* for — deliberately different from what the API returns.
LOCATIONS = [
    Location(resort_id=1,  latitude=60.9282,  longitude=8.3835),    # Hemsedal, Norway
    Location(resort_id=3,  latitude=47.0578,  longitude=9.8282),    # Golm, Austria
    Location(resort_id=7,  latitude=-39.6710, longitude=176.8767),  # "Porter", New Zealand
]


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


def test_parse_produces_one_row_per_resort_per_day(payload):
    rows = parse_batch(payload, LOCATIONS)
    assert len(rows) == 3 * 4, "grain must be one row per resort per day"
    assert {r["resort_id"] for r in rows} == {1, 3, 7}


def test_resort_id_is_carried_by_position_not_by_coordinates(payload):
    """The API snaps coordinates to its grid, so position is the only reliable key."""
    rows = parse_batch(payload, LOCATIONS)
    first = rows[0]

    assert first["resort_id"] == 1
    assert first["req_latitude"] == 60.9282       # what we asked for
    assert first["grid_latitude"] == 60.913883    # what we got back
    assert first["req_latitude"] != first["grid_latitude"], (
        "if these are ever equal the test has stopped proving anything")


def test_both_coordinate_pairs_are_retained(payload):
    """Keeping the requested and returned coordinates makes the offset auditable."""
    rows = parse_batch(payload, LOCATIONS)
    for r in rows:
        assert r["req_latitude"] is not None and r["grid_latitude"] is not None
        assert r["req_longitude"] is not None and r["grid_longitude"] is not None


def test_missing_location_id_on_first_element_does_not_break_alignment(payload):
    """Open-Meteo omits `location_id` on index 0. We must not depend on it."""
    assert "location_id" not in payload[0]
    assert payload[1]["location_id"] == 1

    rows = parse_batch(payload, LOCATIONS)
    # Golm is the second location; its grid cell is at 2129 m.
    golm = [r for r in rows if r["resort_id"] == 3]
    assert all(r["grid_elevation_m"] == 2129.0 for r in golm)


def test_grid_elevation_flags_bad_source_coordinates(payload):
    """A free data-quality check: the grid elevation should look like a ski resort.

    Resort 7 is listed in the source with a 1300-1980 m elevation range, but its grid cell
    comes back at 14 m — the coordinates in the source file point somewhere near sea level.
    """
    rows = parse_batch(payload, LOCATIONS)
    porter = next(r for r in rows if r["resort_id"] == 7)
    assert porter["grid_elevation_m"] == 14.0

    source_lowest_point_m = 1300
    gap = source_lowest_point_m - porter["grid_elevation_m"]
    assert gap > 1000, "this resort's coordinates are implausible and should be flagged"


def test_length_mismatch_refuses_to_guess(payload):
    """Fewer responses than requests means alignment is unknowable — fail, don't improvise."""
    with pytest.raises(ValueError, match="Row alignment cannot be trusted"):
        parse_batch(payload, LOCATIONS + [Location(99, 0.0, 0.0)])


def test_values_are_passed_through_unchanged(payload):
    rows = parse_batch(payload, LOCATIONS)
    hemsedal_day1 = rows[0]
    assert hemsedal_day1["weather_date"] == "2024-01-01"
    assert hemsedal_day1["temp_max_c"] == -11.6
    assert hemsedal_day1["snowfall_cm"] == 3.57
    assert hemsedal_day1["precipitation_mm"] == 5.10


# --------------------------------------------------------------------------- transport
def _client_returning(*responses: httpx.Response) -> httpx.Client:
    """A client that plays back the given responses in order."""
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(it)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retries_on_429_then_succeeds(payload):
    client = _client_returning(
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=payload),
    )
    out = fetch_batch(LOCATIONS, "2024-01-01", "2024-01-04",
                      client=client, use_cache=False, pause_s=0)
    assert len(out) == 3


def test_does_not_retry_a_400(payload):
    """A malformed request will be malformed on the retry too. Fail loudly instead."""
    client = _client_returning(
        httpx.Response(400, json={"reason": "Value out of allowed range"}),
    )
    with pytest.raises(ValueError, match="Value out of allowed range"):
        fetch_batch(LOCATIONS, "1850-01-01", "1850-01-04",
                    client=client, use_cache=False, pause_s=0)


def test_gives_up_after_max_retries():
    client = _client_returning(*[httpx.Response(503) for _ in range(3)])
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        fetch_batch(LOCATIONS, "2024-01-01", "2024-01-04",
                    client=client, use_cache=False, max_retries=3, pause_s=0)


def test_single_location_response_is_normalised_to_a_list(payload):
    """One coordinate returns an object, several return an array. Downstream sees a list."""
    client = _client_returning(httpx.Response(200, json=payload[0]))
    out = fetch_batch(LOCATIONS[:1], "2024-01-01", "2024-01-04",
                      client=client, use_cache=False, pause_s=0)
    assert isinstance(out, list) and len(out) == 1


def test_cache_prevents_a_second_network_call(payload, tmp_path, monkeypatch):
    """A batch already fetched is never fetched again — re-runs are free and polite."""
    monkeypatch.setattr("alpine.weather.CACHE", tmp_path)

    client = _client_returning(httpx.Response(200, json=payload))
    first = fetch_batch(LOCATIONS, "2024-01-01", "2024-01-04",
                        client=client, use_cache=True, pause_s=0)

    # The mock has no responses left; a second network call would raise StopIteration.
    second = fetch_batch(LOCATIONS, "2024-01-01", "2024-01-04",
                         client=client, use_cache=True, pause_s=0)
    assert first == second
    assert len(list(tmp_path.glob("openmeteo_*.json"))) == 1
