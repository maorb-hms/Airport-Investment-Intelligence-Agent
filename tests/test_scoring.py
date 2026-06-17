"""
Unit tests for the deterministic scoring engine (``test_tool.py``).

These tests exercise the *business logic* — the KPI math and the composite
ExpansionScore — not the UI or the AI layer. They are hermetic: the two data
sources (OurAirports CSVs and the OpenSky API) are monkeypatched with tiny
fixtures, so the tests run offline, fast, and deterministically.

Run with:  ``pytest``  (or click "Run Python File" in VS Code).
"""

import json
import math
import os
import sys
from typing import Any, Callable, Dict, List, Tuple

import pytest

# Allow running this file directly (VS Code "Run Python File") as well as via pytest:
# put the project root on sys.path so `import test_tool` resolves either way.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_tool

# A flight record and a fetched-flight list, named for readable annotations below.
Flight = Dict[str, Any]
FlightList = List[Flight]


# ---------------------------------------------------------------------------
# Pure KPI primitives (take data as arguments — no patching needed)
# ---------------------------------------------------------------------------

def test_traffic_volume_is_movements_per_day() -> None:
    """TrafficVolume averages total movements over the window: 14 flights / 7 days = 2.0/day."""
    flights: FlightList = [{} for _ in range(14)]
    assert test_tool.traffic_volume(flights, 7) == 2.0


def test_traffic_volume_guards_zero_window() -> None:
    """A zero-day window returns 0.0 instead of dividing by zero."""
    assert test_tool.traffic_volume([{}, {}], 0) == 0.0


def test_great_circle_distance_known_values() -> None:
    """The Haversine helper returns 0 for identical points and ~10007 km for a
    quarter-turn along the equator ((pi/2) x Earth radius)."""
    assert test_tool._great_circle_distance(0, 0, 0, 0) == pytest.approx(0.0)
    quarter_turn_km: float = test_tool._great_circle_distance(0, 0, 0, 90)
    assert quarter_turn_km == pytest.approx(math.pi / 2 * 6371, rel=1e-3)


def test_utilization_ratio_and_zero_capacity_guard() -> None:
    """Utilization is traffic / capacity, and returns 0.0 when capacity is non-positive."""
    assert test_tool.utilization(100.0, 200.0) == pytest.approx(0.5)
    assert test_tool.utilization(100.0, 0.0) == 0.0


def test_peak_saturation_ratio_and_zero_guard() -> None:
    """PeakSaturation is peak-hour load / hourly capacity, guarding against zero capacity."""
    assert test_tool.peak_saturation(10, 20.0) == pytest.approx(0.5)
    assert test_tool.peak_saturation(10, 0.0) == 0.0


def test_peak_load_buckets_dep_by_firstseen_arr_by_lastseen() -> None:
    """PeakLoad is the busiest single clock-hour. Departures bucket by ``firstSeen`` and
    arrivals by ``lastSeen``; 3 departures + 2 arrivals share one hour (=5), while a
    departure in a different hour must not inflate that peak."""
    base: int = 1_000_000  # an arbitrary unix second
    hour: int = 3600
    departures: FlightList = [{"firstSeen": base + i} for i in range(3)]
    arrivals: FlightList = [{"lastSeen": base + i} for i in range(2)]
    departures.append({"firstSeen": base + hour})  # a second, quieter hour
    assert test_tool.peak_load(departures, arrivals) == 5


def test_distinct_destinations_counts_unique() -> None:
    """DistinctDestinations counts unique non-empty ``estArrivalAirport`` codes."""
    departures: FlightList = [
        {"estArrivalAirport": "KJFK"},
        {"estArrivalAirport": "KJFK"},
        {"estArrivalAirport": "KBOS"},
        {"estArrivalAirport": ""},  # empty → ignored
    ]
    assert test_tool.distinct_destinations(departures) == 2


def test_compute_growth_basic_and_empty_baseline() -> None:
    """Growth is the relative change vs. the baseline window (20 vs. 10 movements = +100%).
    An empty baseline returns ``(None, False)`` — missing data is never treated as zero growth."""
    recent: FlightList = [{} for _ in range(20)]
    baseline: FlightList = [{} for _ in range(10)]
    growth, valid = test_tool.compute_growth(recent, baseline, 7)
    assert valid is True
    assert growth == pytest.approx(1.0)

    growth_none, valid_none = test_tool.compute_growth(recent, [], 7)
    assert growth_none is None
    assert valid_none is False


def test_hourly_clipping_fraction_and_zero_guard() -> None:
    """HourlyClipping is the fraction of operating hours at >= 90% of hourly capacity.
    With capacity 10 (threshold 9), one hour of 9 movements over an 18-hour day = 1/18.
    Zero capacity returns 0.0."""
    base: int = 1_000_000
    departures: FlightList = [{"firstSeen": base} for _ in range(9)]
    result: float = test_tool.hourly_clipping(departures, [], hourly_cap=10.0, window_days=1)
    assert result == pytest.approx(1 / 18)
    assert test_tool.hourly_clipping(departures, [], hourly_cap=0.0, window_days=1) == 0.0


def test_unmet_demand_formula() -> None:
    """UnmetDemand = min(Utilization, 1) x max(0, Growth) + HourlyClipping.
    Here: min(0.8,1) x max(0,0.5) + 0.1 = 0.4 + 0.1 = 0.5."""
    assert test_tool._unmet_demand_kpi(0.8, 0.5, 0.1) == pytest.approx(0.5)


def test_unmet_demand_clamps_utilization_and_floors_negative_growth() -> None:
    """Utilization above 1.0 is clamped and negative growth is floored to 0, so an
    over-capacity airport with shrinking traffic shows only its clipping term (0.2)."""
    assert test_tool._unmet_demand_kpi(1.5, -0.3, 0.2) == pytest.approx(0.2)


def test_unmet_demand_handles_missing_growth() -> None:
    """When growth is ``None`` (no baseline), its term contributes 0 and clipping passes through."""
    assert test_tool._unmet_demand_kpi(0.9, None, 0.15) == pytest.approx(0.15)


def test_confidence_score_fractions() -> None:
    """Confidence is the share of flights carrying both estimated airports. Two complete
    flights → 1.0; one complete + one partial → 0.5; an empty list → 0.0."""
    both: Flight = {"estDepartureAirport": "A", "estArrivalAirport": "B"}
    partial: Flight = {"estDepartureAirport": "A", "estArrivalAirport": ""}
    assert test_tool.confidence_score([both, both]) == pytest.approx(1.0)
    assert test_tool.confidence_score([both, partial]) == pytest.approx(0.5)
    assert test_tool.confidence_score([]) == 0.0


# ---------------------------------------------------------------------------
# Category bands (boundaries matter — they drive the agent's wording)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (0.49, "Low"), (0.50, "Moderate"), (0.84, "Moderate"),
    (0.85, "High"), (1.00, "High"), (1.01, "Over capacity"),
])
def test_utilization_bands(value: float, expected: str) -> None:
    """Utilization maps to the documented bands at the exact §3 boundaries
    (<0.50 Low, 0.50–0.85 Moderate, 0.85–1.00 High, >1.00 Over capacity)."""
    assert test_tool._band_utilization(value) == expected


@pytest.mark.parametrize("value,expected", [
    (0.59, "Comfortable"), (0.60, "Busy"), (0.90, "Busy"), (0.91, "Saturated"),
])
def test_peak_saturation_bands(value: float, expected: str) -> None:
    """PeakSaturation maps to its bands at the boundaries
    (<0.60 Comfortable, 0.60–0.90 Busy, >0.90 Saturated)."""
    assert test_tool._band_peak_saturation(value) == expected


@pytest.mark.parametrize("value,expected", [
    (0.79, "Moderate"), (0.80, "High"), (0.59, "Low"), (0.60, "Moderate"),
])
def test_confidence_bands(value: float, expected: str) -> None:
    """Confidence maps to High/Moderate/Low at the boundaries (>=0.80 High, >=0.60 Moderate)."""
    assert test_tool._band_confidence(value) == expected


# ---------------------------------------------------------------------------
# Capacity index (monkeypatch the runways CSV loader)
# ---------------------------------------------------------------------------

def _runways(*lengths_closed: Tuple[int, int]) -> List[Dict[str, str]]:
    """Build a fake ``runways.csv`` table for airport ``KTEST``.

    Args:
        *lengths_closed: One ``(length_ft, closed)`` pair per runway, where ``closed``
            is 1 for a closed runway and 0 otherwise.

    Returns:
        Runway rows shaped like the OurAirports CSV columns the engine reads.
    """
    return [
        {"airport_ident": "KTEST", "length_ft": str(length), "closed": str(closed)}
        for length, closed in lengths_closed
    ]


def test_capacity_index_heavy_factor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A longest runway >= 9000 ft selects the 'heavy' length factor (1.1), so
    2 runways → CapacityIndex 2x30x18x1.1 = 1188/day and HourlyCapacity 2x30x1.1 = 66/hr."""
    monkeypatch.setattr(test_tool, "_load_runways", lambda: _runways((10000, 0), (8000, 0)))
    assert test_tool.capacity_index("KTEST") == pytest.approx(1188.0)
    assert test_tool.hourly_capacity("KTEST") == pytest.approx(66.0)


def test_capacity_index_short_factor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A longest runway < 6000 ft selects the 'short' factor (0.7): 1x30x18x0.7."""
    monkeypatch.setattr(test_tool, "_load_runways", lambda: _runways((5000, 0)))
    assert test_tool.capacity_index("KTEST") == pytest.approx(1 * 30 * 18 * 0.7)


def test_capacity_index_normal_factor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A longest runway between 6000 and 9000 ft selects the 'normal' factor (1.0): 1x30x18."""
    monkeypatch.setattr(test_tool, "_load_runways", lambda: _runways((7000, 0)))
    assert test_tool.capacity_index("KTEST") == pytest.approx(1 * 30 * 18 * 1.0)


def test_capacity_index_ignores_closed_runways(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed runways are excluded, so only the open 10000 ft runway counts (factor 1.1)."""
    monkeypatch.setattr(test_tool, "_load_runways", lambda: _runways((10000, 0), (5000, 1)))
    assert test_tool.capacity_index("KTEST") == pytest.approx(1 * 30 * 18 * 1.1)


def test_capacity_index_zero_when_all_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With every runway closed there is no usable capacity, so CapacityIndex is 0.0."""
    monkeypatch.setattr(test_tool, "_load_runways", lambda: _runways((9000, 1)))
    assert test_tool.capacity_index("KTEST") == 0.0


# ---------------------------------------------------------------------------
# Long-haul classification (monkeypatch the airports CSV loader)
# ---------------------------------------------------------------------------

# Origin at (0,0); KFAR is ~10007 km away (long), KNEAR is ~111 km away (short).
LONG_HAUL_AIRPORTS: List[Dict[str, str]] = [
    {"ident": "KORG", "latitude_deg": "0", "longitude_deg": "0"},
    {"ident": "KFAR", "latitude_deg": "0", "longitude_deg": "90"},
    {"ident": "KNEAR", "latitude_deg": "0", "longitude_deg": "1"},
]


def test_long_haul_share_distance_based(monkeypatch: pytest.MonkeyPatch) -> None:
    """When destination coordinates are known, haul length is classified by great-circle
    distance (>= 4000 km = long). One far + one near departure → a 0.5 long-haul share."""
    monkeypatch.setattr(test_tool, "_load_airports", lambda: LONG_HAUL_AIRPORTS)
    departures: FlightList = [
        {"estArrivalAirport": "KFAR", "firstSeen": 1000, "lastSeen": 1000 + 3600},
        {"estArrivalAirport": "KNEAR", "firstSeen": 1000, "lastSeen": 1000 + 3600},
    ]
    share, breakdown = test_tool._long_haul_share_kpi(departures, "KORG")
    assert share == pytest.approx(0.5)
    assert breakdown["distance_classified"] == 2
    assert breakdown["long_haul_by_distance"] == 1
    assert breakdown["distance_based_share"] == pytest.approx(0.5)


def test_long_haul_share_duration_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the destination is unknown (no coordinates), the engine falls back to flight
    duration (>= 360 min = long). A 7-hour flight to an unlisted airport → a 1.0 share."""
    monkeypatch.setattr(test_tool, "_load_airports", lambda: LONG_HAUL_AIRPORTS)
    seven_hours: int = 7 * 3600
    departures: FlightList = [
        {"estArrivalAirport": "ZZZZ", "firstSeen": 1000, "lastSeen": 1000 + seven_hours},
    ]
    share, breakdown = test_tool._long_haul_share_kpi(departures, "KORG")
    assert share == pytest.approx(1.0)
    assert breakdown["duration_classified"] == 1
    assert breakdown["distance_classified"] == 0


# ---------------------------------------------------------------------------
# resolve_airport (monkeypatch the airports CSV loader)
# ---------------------------------------------------------------------------

RESOLVE_AIRPORTS: List[Dict[str, str]] = [
    {"ident": "KSFO", "iata_code": "SFO", "name": "San Francisco Intl", "municipality": "San Francisco"},
    {"ident": "KBOS", "iata_code": "BOS", "name": "Boston Logan Intl", "municipality": "Boston"},
]


def test_resolve_airport_by_icao_iata_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An airport resolves by ICAO code, by IATA code (case-insensitive), and by a
    case-insensitive substring of its name."""
    monkeypatch.setattr(test_tool, "_load_airports", lambda: RESOLVE_AIRPORTS)
    assert test_tool.resolve_airport("KBOS")["ident"] == "KBOS"
    assert test_tool.resolve_airport("sfo")["ident"] == "KSFO"
    assert test_tool.resolve_airport("Logan")["ident"] == "KBOS"


def test_resolve_airport_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised query raises ``ValueError`` — the graceful-degradation path the
    agent surfaces to the user as a plain "couldn't find that airport" message."""
    monkeypatch.setattr(test_tool, "_load_airports", lambda: RESOLVE_AIRPORTS)
    with pytest.raises(ValueError):
        test_tool.resolve_airport("Nowhere International ZZZ")


# ---------------------------------------------------------------------------
# rank_region — the composite ExpansionScore, end-to-end but offline
# ---------------------------------------------------------------------------

RANK_AIRPORTS: List[Dict[str, str]] = [
    {"ident": "KAAA", "iata_code": "AAA", "name": "Alpha Intl", "municipality": "Alpha",
     "iso_region": "US-XX", "type": "large_airport", "scheduled_service": "yes",
     "latitude_deg": "10", "longitude_deg": "10"},
    {"ident": "KBBB", "iata_code": "BBB", "name": "Bravo Intl", "municipality": "Bravo",
     "iso_region": "US-XX", "type": "large_airport", "scheduled_service": "yes",
     "latitude_deg": "20", "longitude_deg": "20"},
]

RANK_RUNWAYS: List[Dict[str, str]] = [
    {"airport_ident": "KAAA", "length_ft": "10000", "closed": "0"},
    {"airport_ident": "KBBB", "length_ft": "10000", "closed": "0"},
]


def _fake_opensky(busy_icao: str) -> Callable[[str, int, int, str], FlightList]:
    """Build a stand-in for ``_get_opensky_flights``.

    Args:
        busy_icao: The airport that should look busier; it receives 5x the movements
            of every other airport on each fetch.

    Returns:
        A function with the same ``(icao, begin_unix, end_unix, kind)`` signature as the
        real fetcher, returning synthetic flight records.
    """
    def _fetch(icao: str, begin_unix: int, end_unix: int, kind: str) -> FlightList:
        count: int = 5 if icao == busy_icao else 1
        return [
            {
                "firstSeen": begin_unix + i * 60,
                "lastSeen": begin_unix + i * 60 + 3600,
                "estDepartureAirport": icao,
                "estArrivalAirport": "KZZZ",
            }
            for i in range(count)
        ]
    return _fetch


@pytest.fixture
def patched_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch both data sources so ``rank_region`` runs fully offline: a 2-airport region,
    matching runways, and a synthetic OpenSky feed where KAAA is the busier airport."""
    monkeypatch.setattr(test_tool, "_load_airports", lambda: RANK_AIRPORTS)
    monkeypatch.setattr(test_tool, "_load_runways", lambda: RANK_RUNWAYS)
    monkeypatch.setattr(test_tool, "_get_opensky_flights", _fake_opensky("KAAA"))


def test_rank_region_returns_valid_json_and_ranks_busier_airport_first(patched_rank: None) -> None:
    """rank_region returns parseable JSON for the candidate set, assigns ranks 1..N in
    descending score order, and places the busier airport (KAAA, higher utilization) first."""
    result: Dict[str, Any] = json.loads(test_tool.rank_region(["US-XX"]))

    assert result["data_available"] is True
    assert result["candidate_count"] == 2
    ranked: List[Dict[str, Any]] = result["ranked"]
    assert len(ranked) == 2

    assert ranked[0]["rank"] == 1
    assert ranked[1]["rank"] == 2
    assert ranked[0]["expansion_score"] >= ranked[1]["expansion_score"]
    assert ranked[0]["airport"]["icao"] == "KAAA"


def test_rank_region_scores_are_bounded_0_to_100(patched_rank: None) -> None:
    """Every ExpansionScore stays within the documented 0–100 range."""
    result: Dict[str, Any] = json.loads(test_tool.rank_region(["US-XX"]))
    for entry in result["ranked"]:
        assert 0.0 <= entry["expansion_score"] <= 100.0


def test_rank_region_weights_sum_to_one(patched_rank: None) -> None:
    """The documented base weights (0.40 / 0.30 / 0.20 / 0.10) sum to exactly 1.0."""
    result: Dict[str, Any] = json.loads(test_tool.rank_region(["US-XX"]))
    assert sum(result["base_weights"].values()) == pytest.approx(1.0)


if __name__ == "__main__":
    # So "Run Python File" in VS Code actually runs the test suite (not nothing).
    # The usual entry point is still: `pytest` from the project root.
    raise SystemExit(pytest.main([__file__, "-v"]))
