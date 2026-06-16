"""Deterministic Layer (pure Python SSOT): data ingestion + all KPI math, returns strict JSON; no AI/Anthropic deps (architecture.md §3)."""

import os
import json
import csv
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import requests
from math import radians, sin, cos, sqrt, atan2
from dotenv import load_dotenv


# ===== Cache & Data Source Constants =====
CACHE_DIR: Path = Path(".cache")
OURAIRPORTS_CACHE_DIR: Path = CACHE_DIR / "ourairports"
OURAIRPORTS_REFRESH_DAYS: int = 7

AIRPORTS_CSV_URL: str = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_CSV_URL: str = "https://davidmegginson.github.io/ourairports-data/runways.csv"

# Candidate universe filter (data-and-apis.md §2.1)
CANDIDATE_AIRPORT_TYPES: set[str] = {"large_airport", "medium_airport"}
CANDIDATE_SCHEDULED_SERVICE: str = "yes"

# New England region codes (data-and-apis.md §2.1)
NEW_ENGLAND_REGIONS: set[str] = {"US-ME", "US-NH", "US-VT", "US-MA", "US-RI", "US-CT"}

# Metro disambiguation aliases (data-and-apis.md §2.1)
METRO_ALIASES: Dict[str, str] = {
    "LA": "KLAX",
    "Los Angeles": "KLAX",
    "Santa Ana": "KSNA",
    "Anchorage": "PANC",
    "SFO": "KSFO",
    "San Francisco": "KSFO",
}
# Case-insensitive lookup map (lowercased key -> ICAO) so "la"/"sfo" also resolve.
_METRO_ALIASES_LOWER: Dict[str, str] = {k.lower(): v for k, v in METRO_ALIASES.items()}

# OpenSky Network API constants (data-and-apis.md §1)
OPENSKY_AUTH_URL: str = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_API_BASE: str = "https://opensky-network.org/api"
OPENSKY_CACHE_DIR: Path = CACHE_DIR / "opensky"
OPENSKY_TOKEN_EXPIRY_BUFFER_SEC: int = 60
OPENSKY_MAX_RETRIES: int = 4  # retries on HTTP 429 (rate limit) with backoff
OPENSKY_MAX_BACKOFF_SEC: int = 30  # cap per-retry sleep
OPENSKY_MAX_WORKERS: int = 10  # max concurrent OpenSky chunk fetches (bounds rate-limit pressure)
RANK_GROWTH_TOP_K: int = 8  # rank_region computes the 90-day Growth baseline only for the top-K busiest candidates
STABLE_WINDOW_LAG_DAYS: int = 2
WINDOW_DAYS: int = 7
MAX_API_INTERVAL_DAYS: int = 1

# ===== KPI Tunable Constants (scoring-and-kpis.md §0) =====
RUNWAY_THROUGHPUT_PER_HOUR: int = 30  # movements/hr for one runway
OPERATING_HOURS: int = 18  # active hours/day
LONGRUNWAY_SHORT_FT: int = 6000  # below = small-aircraft only
LONGRUNWAY_HEAVY_FT: int = 9000  # above = efficiently handles heavies
LENGTH_FACTOR: Dict[str, float] = {"short": 0.7, "normal": 1.0, "heavy": 1.1}
LONG_HAUL_MIN_KM: float = 4000.0  # great-circle distance threshold for "long-haul"
LONG_HAUL_MIN_MINUTES: int = 360  # duration fallback when coords unavailable
BASELINE_LAG_DAYS: int = 90  # Growth baseline ends this many days before the current window
CONFIDENCE_PENALTY_ON_GROWTH_NULL: float = 0.85  # penalty when baseline is empty

# OpenSky OAuth2 client-credentials (from .env)
load_dotenv()
OPENSKY_CLIENT_ID: str = os.getenv("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET: str = os.getenv("OPENSKY_CLIENT_SECRET", "")

# In-memory token cache (data-and-apis.md §1.1: refresh on expiry or on 401)
_cached_oauth_token: Optional[str] = None
_token_expiry_time: Optional[datetime] = None
_token_lock: threading.Lock = threading.Lock()  # serialize token refresh across concurrent fetches


def _ensure_cache_dir() -> None:
    """
    Create cache directories if they don't exist.

    Ensures the OurAirports and OpenSky cache directories exist before any file I/O operations.
    """
    OURAIRPORTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OPENSKY_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_oauth_token() -> str:
    """
    Retrieve or refresh OAuth2 access token from OpenSky.

    Uses in-memory caching with expiry tracking. Automatically refreshes on expiry or on 401.
    Credentials are read from OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET (.env).

    Returns:
        OAuth2 access token (Bearer token).

    Raises:
        ValueError: If credentials are not set in .env.
        requests.RequestException: If the token request fails.
    """
    global _cached_oauth_token, _token_expiry_time

    # Serialize refresh: under concurrency, only one thread fetches a new token;
    # the rest block here and then see the freshly cached token (double-checked below).
    with _token_lock:
        # Check if cached token is still valid
        if _cached_oauth_token is not None and _token_expiry_time is not None:
            time_remaining: timedelta = _token_expiry_time - datetime.now()
            is_token_valid: bool = time_remaining.total_seconds() > OPENSKY_TOKEN_EXPIRY_BUFFER_SEC
            if is_token_valid:
                return _cached_oauth_token

        # Credentials not set
        if not OPENSKY_CLIENT_ID or not OPENSKY_CLIENT_SECRET:
            error_msg: str = "OpenSky credentials not set: add OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET to .env"
            raise ValueError(error_msg)

        # Request new token
        token_request_data: Dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET,
        }
        token_response: requests.Response = requests.post(OPENSKY_AUTH_URL, data=token_request_data, timeout=30)
        token_response.raise_for_status()

        token_json: Dict[str, Any] = token_response.json()
        access_token: str = token_json["access_token"]
        expires_in_seconds: int = token_json["expires_in"]

        # Cache token with expiry time
        _cached_oauth_token = access_token
        _token_expiry_time = datetime.now() + timedelta(seconds=expires_in_seconds)

        return access_token


def _opensky_get(endpoint: str, params: Dict[str, Any]) -> requests.Response:
    """
    GET an OpenSky endpoint with auth, refreshing the token on 401 and backing
    off on 429 (rate limit).

    On 401 the in-memory token is invalidated and re-fetched (data-and-apis.md §1.1).
    On 429 the client sleeps (honouring a numeric Retry-After header when present,
    else exponential backoff capped at OPENSKY_MAX_BACKOFF_SEC) and retries up to
    OPENSKY_MAX_RETRIES times before surfacing the error.

    Args:
        endpoint: Full URL of the OpenSky endpoint.
        params: Query parameters.

    Returns:
        A successful requests.Response.

    Raises:
        requests.RequestException: If the request still fails after retries.
    """
    global _cached_oauth_token, _token_expiry_time

    last_response: Optional[requests.Response] = None
    for attempt in range(OPENSKY_MAX_RETRIES):
        access_token: str = _get_oauth_token()
        auth_headers: Dict[str, str] = {"Authorization": f"Bearer {access_token}"}
        response: requests.Response = requests.get(endpoint, params=params, headers=auth_headers, timeout=30)
        last_response = response

        if response.status_code == 401:
            # Token rejected — force a refresh and retry.
            _cached_oauth_token = None
            _token_expiry_time = None
            continue

        if response.status_code == 429:
            retry_after_header: str = response.headers.get("Retry-After", "")
            if retry_after_header.isdigit():
                wait_seconds: int = int(retry_after_header)
            else:
                wait_seconds = 2 * (2 ** attempt)
            time.sleep(min(wait_seconds, OPENSKY_MAX_BACKOFF_SEC))
            continue

        response.raise_for_status()
        return response

    # Retries exhausted — raise on the last response.
    if last_response is not None:
        last_response.raise_for_status()
    raise requests.RequestException("OpenSky request failed with no response")


def _get_opensky_flights(
    icao: str, begin_unix: int, end_unix: int, kind: str
) -> List[Dict[str, Any]]:
    """
    Fetch flights from OpenSky for a single day window.

    Retrieves either departures or arrivals for a single airport within a 1-day window.
    Responses are cached on disk (key: airport+window+kind) to avoid re-spending credits.
    Handles 401 by refreshing the token and retrying.

    Args:
        icao: ICAO airport identifier (e.g., "KSFO").
        begin_unix: Window start time in Unix epoch seconds.
        end_unix: Window end time in Unix epoch seconds.
        kind: "departure" or "arrival".

    Returns:
        List of flight records from the API response.

    Raises:
        ValueError: If kind is not "departure" or "arrival".
        requests.RequestException: If the API request fails.
    """
    if kind not in ("departure", "arrival"):
        error_msg: str = f"Invalid kind: '{kind}'; must be 'departure' or 'arrival'"
        raise ValueError(error_msg)

    # Build cache key: airport_window_kind (using Unix timestamps for uniqueness)
    cache_key: str = f"{icao}_{begin_unix}_{end_unix}_{kind}"
    cache_file_path: Path = OPENSKY_CACHE_DIR / f"{cache_key}.json"

    # Check disk cache first
    if cache_file_path.exists():
        with open(cache_file_path, "r", encoding="utf-8") as f:
            cached_flights: List[Dict[str, Any]] = json.load(f)
            return cached_flights

    # Build API request
    api_endpoint: str = f"{OPENSKY_API_BASE}/flights/{kind}"
    api_params: Dict[str, Any] = {
        "airport": icao,
        "begin": begin_unix,
        "end": end_unix,
    }

    # Issue the request with token-refresh (401) and rate-limit (429) handling.
    api_response: requests.Response = _opensky_get(api_endpoint, api_params)

    # Parse response (array of flight objects or empty array)
    flights: List[Dict[str, Any]] = api_response.json()
    if not isinstance(flights, list):
        flights = []

    # Cache on disk for future requests
    with open(cache_file_path, "w", encoding="utf-8") as f:
        json.dump(flights, f)

    return flights


ChunkTask = Tuple[str, int, int, str]  # (icao, begin_unix, end_unix, kind)


def _day_chunks(begin_unix: int, end_unix: int) -> List[Tuple[int, int]]:
    """Split a window into consecutive 1-day (begin, end) chunks (OpenSky's hard per-call limit)."""
    seconds_per_day: int = 86400
    chunks: List[Tuple[int, int]] = []
    current_begin: int = begin_unix
    while current_begin < end_unix:
        current_end: int = min(current_begin + seconds_per_day, end_unix)
        chunks.append((current_begin, current_end))
        current_begin = current_end
    return chunks


def _fetch_chunks_concurrent(
    tasks: List[ChunkTask],
) -> Tuple[Dict[ChunkTask, List[Dict[str, Any]]], List[str]]:
    """
    Fetch many 1-day chunk requests concurrently (bounded by OPENSKY_MAX_WORKERS).

    Each task is a cached, retry-aware single-day fetch. A failing task is recorded
    in the errors list and yields an empty result rather than aborting the batch —
    so one airport's failure can't sink a whole region's ranking. Cache hits cost
    no network and return near-instantly.

    Args:
        tasks: List of (icao, begin_unix, end_unix, kind) chunk tasks.

    Returns:
        (results, errors): results maps each task to its flight list (empty on
        failure); errors holds one human-readable string per failed task.
    """
    results: Dict[ChunkTask, List[Dict[str, Any]]] = {}
    errors: List[str] = []
    if not tasks:
        return results, errors

    worker_count: int = min(OPENSKY_MAX_WORKERS, len(tasks))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_task: Dict[Any, ChunkTask] = {
            executor.submit(_get_opensky_flights, *task): task for task in tasks
        }
        for future in as_completed(future_to_task):
            task: ChunkTask = future_to_task[future]
            try:
                results[task] = future.result()
            except requests.RequestException as exc:
                results[task] = []
                errors.append(f"{task[0]} {task[3]} {task[1]}-{task[2]} failed: {exc}")
    return results, errors


def get_flights(
    icao: str, begin_unix: int, end_unix: int, kind: str
) -> List[Dict[str, Any]]:
    """
    Fetch flights for a multi-day window by chunking into 1-day calls.

    OpenSky's /flights/* endpoints reject intervals > 1 day (data-and-apis.md §1.2).
    This function splits the requested window into consecutive 1-day chunks, fetches each,
    and aggregates the results. Caching per chunk saves credits on overlapping queries.

    Args:
        icao: ICAO airport identifier (e.g., "KSFO").
        begin_unix: Window start time in Unix epoch seconds.
        end_unix: Window end time in Unix epoch seconds.
        kind: "departure" or "arrival".

    Returns:
        Aggregated list of flight records across all chunks.

    Raises:
        ValueError: If kind is not "departure" or "arrival", or if window spans > ~2 months.
        requests.RequestException: If any API request fails.
    """
    if kind not in ("departure", "arrival"):
        error_msg: str = f"Invalid kind: '{kind}'; must be 'departure' or 'arrival'"
        raise ValueError(error_msg)

    chunks: List[Tuple[int, int]] = _day_chunks(begin_unix, end_unix)
    tasks: List[ChunkTask] = [(icao, chunk_begin, chunk_end, kind) for chunk_begin, chunk_end in chunks]
    results, errors = _fetch_chunks_concurrent(tasks)

    # Total failure (errors and nothing fetched) → raise so callers can log it;
    # a partial failure returns the days we did get (one missing day only mildly undercounts).
    if errors and all(len(flights) == 0 for flights in results.values()):
        raise requests.RequestException("; ".join(errors))

    all_flights: List[Dict[str, Any]] = []
    for task in tasks:
        all_flights.extend(results.get(task, []))
    return all_flights



def _is_cache_stale(cache_path: Path, max_age_days: int = OURAIRPORTS_REFRESH_DAYS) -> bool:
    """
    Check if a cache file is missing or older than max_age_days.

    Args:
        cache_path: Path to the cached file.
        max_age_days: Maximum age of cache in days before considered stale.

    Returns:
        True if cache is missing or older than max_age_days; False otherwise.
    """
    if not cache_path.exists():
        return True
    file_modification_time: datetime = datetime.fromtimestamp(cache_path.stat().st_mtime)
    cache_age: timedelta = datetime.now() - file_modification_time
    is_stale: bool = cache_age > timedelta(days=max_age_days)
    return is_stale


def _fetch_csv(url: str, cache_path: Path) -> List[Dict[str, Any]]:
    """
    Fetch a CSV from a URL and cache it locally; reuse cache if fresh.

    If the cache file exists and is fresh (< OURAIRPORTS_REFRESH_DAYS old), read from disk.
    Otherwise, fetch from the URL, write to cache, and return the parsed data.

    Args:
        url: URL to fetch the CSV from.
        cache_path: Path where the CSV should be cached locally.

    Returns:
        List of dictionaries, one per CSV row.

    Raises:
        requests.RequestException: If the HTTP request fails.
    """
    _ensure_cache_dir()

    cache_is_fresh: bool = not _is_cache_stale(cache_path)
    if cache_is_fresh:
        # Cache is fresh — read from disk
        with open(cache_path, "r", encoding="utf-8") as f:
            cached_data: List[Dict[str, Any]] = list(csv.DictReader(f))
            return cached_data

    # Cache is stale or missing — fetch from URL
    http_response: requests.Response = requests.get(url, timeout=30)
    http_response.raise_for_status()

    # Write to cache
    with open(cache_path, "w", encoding="utf-8", newline="") as f:
        f.write(http_response.text)

    # Parse and return
    csv_reader: csv.DictReader = csv.DictReader(http_response.text.splitlines())
    parsed_rows: List[Dict[str, Any]] = list(csv_reader)
    return parsed_rows


def _load_airports() -> List[Dict[str, Any]]:
    """
    Load airports.csv, using local cache if available and fresh.

    Returns:
        List of airport records from the OurAirports dataset.
    """
    cache_path: Path = OURAIRPORTS_CACHE_DIR / "airports.csv"
    airports: List[Dict[str, Any]] = _fetch_csv(AIRPORTS_CSV_URL, cache_path)
    return airports


def _load_runways() -> List[Dict[str, Any]]:
    """
    Load runways.csv, using local cache if available and fresh.

    Returns:
        List of runway records from the OurAirports dataset.
    """
    cache_path: Path = OURAIRPORTS_CACHE_DIR / "runways.csv"
    runways: List[Dict[str, Any]] = _fetch_csv(RUNWAYS_CSV_URL, cache_path)
    return runways


def _great_circle_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Compute great-circle distance in km between two geographic coordinates.

    Uses the Haversine formula for accurate geodetic distance on Earth's surface.

    Args:
        lat1: Latitude of the first point in decimal degrees.
        lon1: Longitude of the first point in decimal degrees.
        lat2: Latitude of the second point in decimal degrees.
        lon2: Longitude of the second point in decimal degrees.

    Returns:
        Great-circle distance in kilometers.
    """
    earth_radius_km: float = 6371  # Earth radius in km
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(radians, [lat1, lon1, lat2, lon2])

    latitude_delta: float = lat2_rad - lat1_rad
    longitude_delta: float = lon2_rad - lon1_rad

    haversine_a: float = sin(latitude_delta / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(longitude_delta / 2) ** 2
    haversine_c: float = 2 * atan2(sqrt(haversine_a), sqrt(1 - haversine_a))

    distance_km: float = earth_radius_km * haversine_c
    return distance_km


def resolve_airport(query: str) -> Dict[str, Any]:
    """
    Resolve a query string to an airport dictionary with metadata.

    Attempts to match the query string against airport records in the following order:
    1. Metro aliases (e.g., "LA" -> "KLAX")
    2. ICAO code (exact, case-insensitive)
    3. IATA code (exact, case-insensitive)
    4. Airport name (case-insensitive substring)
    5. Municipality name (case-insensitive exact)

    Args:
        query: The airport name, code (ICAO/IATA), or metro alias to resolve.

    Returns:
        Dictionary containing airport metadata: ident (ICAO), iata_code, name,
        latitude_deg, longitude_deg, iso_region, type, municipality, etc.

    Raises:
        ValueError: If the query does not match any airport in the database.
    """
    query_trimmed: str = query.strip()

    # Check metro aliases first (data-and-apis.md §2.1), case-insensitively
    if query_trimmed.lower() in _METRO_ALIASES_LOWER:
        target_icao: str = _METRO_ALIASES_LOWER[query_trimmed.lower()]
        airports: List[Dict[str, Any]] = _load_airports()
        for airport_record in airports:
            if airport_record["ident"] == target_icao:
                return airport_record
        error_msg: str = f"Metro alias '{query_trimmed}' -> '{target_icao}' not found in database"
        raise ValueError(error_msg)

    airports: List[Dict[str, Any]] = _load_airports()
    query_upper: str = query_trimmed.upper()

    # Match by ICAO (exact)
    for airport_record in airports:
        if airport_record["ident"].upper() == query_upper:
            return airport_record

    # Match by IATA (exact)
    for airport_record in airports:
        airport_iata: str = airport_record.get("iata_code", "")
        if airport_iata.upper() == query_upper:
            return airport_record

    # Match by name (case-insensitive contains)
    for airport_record in airports:
        if query_upper in airport_record["name"].upper():
            return airport_record

    # Match by municipality (case-insensitive exact)
    for airport_record in airports:
        airport_municipality: str = airport_record.get("municipality", "")
        if airport_municipality.upper() == query_upper:
            return airport_record

    error_msg: str = f"Airport '{query_trimmed}' not found"
    raise ValueError(error_msg)


def region_airports(region_codes: set[str]) -> List[Dict[str, Any]]:
    """
    Filter airports by region codes and candidate criteria.

    Returns all airports in the specified regions that meet the commercial airport criteria:
    - type in {large_airport, medium_airport}
    - scheduled_service == "yes"

    Args:
        region_codes: Set of ISO region codes (e.g., {"US-MA", "US-CT"}).

    Returns:
        List of airport records meeting the filter criteria.
    """
    airports: List[Dict[str, Any]] = _load_airports()

    filtered_airports: List[Dict[str, Any]] = []
    for airport_record in airports:
        airport_region: str = airport_record["iso_region"]
        airport_type: str = airport_record["type"]
        airport_scheduled_service: str = airport_record.get("scheduled_service", "")

        is_in_region: bool = airport_region in region_codes
        is_commercial_type: bool = airport_type in CANDIDATE_AIRPORT_TYPES
        has_scheduled_service: bool = airport_scheduled_service == CANDIDATE_SCHEDULED_SERVICE

        if is_in_region and is_commercial_type and has_scheduled_service:
            filtered_airports.append(airport_record)

    return filtered_airports


def runway_capacity(icao: str) -> Dict[str, Any]:
    """
    Compute runway capacity metrics for an airport.

    Counts usable (non-closed) runways and identifies the longest runway length.
    Used to derive the CapacityIndex KPI (scoring-and-kpis.md §2).

    Args:
        icao: The ICAO identifier for the airport (e.g., "KSFO").

    Returns:
        Dictionary with keys:
        - airport_ident: The ICAO code
        - usable_runway_count: Number of non-closed runways
        - longest_ft: Longest runway length in feet

    Raises:
        ValueError: If the airport has no runways or all runways are closed.
    """
    runways: List[Dict[str, Any]] = _load_runways()

    # Filter to this airport's runways
    airport_runways: List[Dict[str, Any]] = [r for r in runways if r["airport_ident"] == icao]

    if not airport_runways:
        error_msg: str = f"No runways found for airport '{icao}'"
        raise ValueError(error_msg)

    # Usable runways = not closed (scoring-and-kpis.md §2)
    usable_runways: List[Dict[str, Any]] = [r for r in airport_runways if r.get("closed", "") != "1"]

    if not usable_runways:
        error_msg: str = f"No usable runways for airport '{icao}' (all closed)"
        raise ValueError(error_msg)

    # Longest usable runway
    runway_lengths: List[int] = [int(r["length_ft"]) for r in usable_runways if r.get("length_ft")]
    longest_runway_ft: int = max(runway_lengths) if runway_lengths else 0

    capacity_result: Dict[str, Any] = {
        "airport_ident": icao,
        "usable_runway_count": len(usable_runways),
        "longest_ft": longest_runway_ft,
    }
    return capacity_result


# ===== KPI Functions (scoring-and-kpis.md §1–§7) =====

def traffic_volume(flights: List[Dict[str, Any]], window_days: int) -> float:
    """
    Compute average daily movements (departures + arrivals) over a window.

    Args:
        flights: Combined list of departure and arrival flight records.
        window_days: Number of days in the observation window.

    Returns:
        Average movements per day; 0.0 if window_days <= 0.
    """
    if window_days <= 0:
        return 0.0
    total_movements: int = len(flights)
    return total_movements / window_days


def _hourly_movement_curve(
    departures: List[Dict[str, Any]], arrivals: List[Dict[str, Any]]
) -> Dict[int, int]:
    """
    Build the per-clock-hour movement curve (scoring-and-kpis.md §1 bucketing rule).

    A departure counts in the hour of its firstSeen (its takeoff at this airport);
    an arrival counts in the hour of its lastSeen (its landing at this airport).
    Each movement is counted exactly once. Buckets are absolute UTC hours
    (unix // 3600), so the same clock hour on different days stays distinct.

    Args:
        departures: Departure flight records (bucketed by firstSeen).
        arrivals: Arrival flight records (bucketed by lastSeen).

    Returns:
        Dict mapping absolute-hour bucket -> movements in that hour.
    """
    hourly_movements: Dict[int, int] = {}

    for departure_record in departures:
        first_seen_unix: int = departure_record.get("firstSeen", 0) or 0
        if first_seen_unix > 0:
            first_seen_hour: int = first_seen_unix // 3600
            hourly_movements[first_seen_hour] = hourly_movements.get(first_seen_hour, 0) + 1

    for arrival_record in arrivals:
        last_seen_unix: int = arrival_record.get("lastSeen", 0) or 0
        if last_seen_unix > 0:
            last_seen_hour: int = last_seen_unix // 3600
            hourly_movements[last_seen_hour] = hourly_movements.get(last_seen_hour, 0) + 1

    return hourly_movements


def peak_load(departures: List[Dict[str, Any]], arrivals: List[Dict[str, Any]]) -> int:
    """
    Find the maximum movements in any single clock hour (PeakLoad).

    Departures count in the hour of firstSeen; arrivals in the hour of lastSeen
    (scoring-and-kpis.md §1). Each movement is counted once; PeakLoad is the
    busiest such hour observed across the window.

    Args:
        departures: Departure flight records.
        arrivals: Arrival flight records.

    Returns:
        Maximum movements observed in any clock hour across the window.
    """
    hourly_movements: Dict[int, int] = _hourly_movement_curve(departures, arrivals)
    if not hourly_movements:
        return 0
    return max(hourly_movements.values())


def distinct_destinations(departures: List[Dict[str, Any]]) -> int:
    """
    Count unique destination airports.

    Args:
        departures: List of departure flight records.

    Returns:
        Number of distinct estArrivalAirport codes.
    """
    destinations: set[str] = set()
    for departure_record in departures:
        destination_airport: str = departure_record.get("estArrivalAirport", "")
        if destination_airport:
            destinations.add(destination_airport)

    return len(destinations)


def capacity_index(icao: str) -> float:
    """
    Compute maximum movements per day (CapacityIndex).

    Formula: count(usable_runways) × RUNWAY_THROUGHPUT_PER_HOUR × OPERATING_HOURS × length_factor
    where length_factor depends on the longest runway length.

    Args:
        icao: ICAO airport identifier.

    Returns:
        Maximum movements per day; 0.0 if airport not found or has no usable runways.
    """
    try:
        capacity_metrics: Dict[str, Any] = runway_capacity(icao)
    except ValueError:
        return 0.0

    usable_runway_count: int = capacity_metrics["usable_runway_count"]
    longest_ft: int = capacity_metrics["longest_ft"]

    if longest_ft < LONGRUNWAY_SHORT_FT:
        factor: float = LENGTH_FACTOR["short"]
    elif longest_ft >= LONGRUNWAY_HEAVY_FT:
        factor = LENGTH_FACTOR["heavy"]
    else:
        factor = LENGTH_FACTOR["normal"]

    capacity_val: float = usable_runway_count * RUNWAY_THROUGHPUT_PER_HOUR * OPERATING_HOURS * factor
    return capacity_val


def hourly_capacity(icao: str) -> float:
    """
    Compute maximum movements per hour (HourlyCapacity).

    Formula: count(usable_runways) × RUNWAY_THROUGHPUT_PER_HOUR × length_factor

    Args:
        icao: ICAO airport identifier.

    Returns:
        Maximum movements per hour; 0.0 if airport not found.
    """
    try:
        capacity_metrics: Dict[str, Any] = runway_capacity(icao)
    except ValueError:
        return 0.0

    usable_runway_count: int = capacity_metrics["usable_runway_count"]
    longest_ft: int = capacity_metrics["longest_ft"]

    if longest_ft < LONGRUNWAY_SHORT_FT:
        factor = LENGTH_FACTOR["short"]
    elif longest_ft >= LONGRUNWAY_HEAVY_FT:
        factor = LENGTH_FACTOR["heavy"]
    else:
        factor = LENGTH_FACTOR["normal"]

    hourly_cap: float = usable_runway_count * RUNWAY_THROUGHPUT_PER_HOUR * factor
    return hourly_cap


def utilization(traffic_vol: float, capacity_idx: float) -> float:
    """
    Compute utilization ratio (TrafficVolume / CapacityIndex).

    Args:
        traffic_vol: TrafficVolume (movements/day).
        capacity_idx: CapacityIndex (max movements/day).

    Returns:
        Utilization ratio; 0.0 if capacity <= 0.
    """
    if capacity_idx <= 0:
        return 0.0
    return traffic_vol / capacity_idx


def peak_saturation(peak_load_val: int, hourly_cap: float) -> float:
    """
    Compute peak saturation ratio (PeakLoad / HourlyCapacity).

    Args:
        peak_load_val: PeakLoad (movements in busiest hour).
        hourly_cap: HourlyCapacity (max movements/hour).

    Returns:
        Peak saturation ratio; 0.0 if hourly_cap <= 0.
    """
    if hourly_cap <= 0:
        return 0.0
    return peak_load_val / hourly_cap


def _long_haul_share_kpi(departures: List[Dict[str, Any]], origin_icao: str) -> tuple[float, Dict[str, Any]]:
    """
    Compute fraction of long-haul departures (KPI helper; the public Q3 tool is long_haul_share).

    For each departure, classify as long-haul using:
    - Distance-based (if both airports resolve): great_circle_km >= LONG_HAUL_MIN_KM
    - Duration fallback (if coords unavailable): (lastSeen - firstSeen) / 60 >= LONG_HAUL_MIN_MINUTES

    Args:
        departures: List of departure flight records.
        origin_icao: Origin airport ICAO (for context).

    Returns:
        Tuple of (long_haul_fraction, breakdown_dict) where breakdown_dict contains:
        - long_haul_distance_count: flights classified by distance
        - long_haul_duration_count: flights classified by duration
        - usable_flights: departures with identifiable signals
    """
    airports: List[Dict[str, Any]] = _load_airports()
    airport_dict: Dict[str, Dict[str, Any]] = {a["ident"]: a for a in airports}

    origin_airport: Optional[Dict[str, Any]] = airport_dict.get(origin_icao)
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    if origin_airport and origin_airport.get("latitude_deg") and origin_airport.get("longitude_deg"):
        try:
            origin_lat = float(origin_airport["latitude_deg"])
            origin_lon = float(origin_airport["longitude_deg"])
        except (ValueError, TypeError):
            origin_lat = origin_lon = None

    usable_flights: int = 0
    long_haul_count: int = 0           # headline: distance if coords available, else duration
    distance_classified: int = 0       # flights classified by distance (coords available)
    duration_classified: int = 0       # flights classified by duration fallback (no coords)
    long_haul_by_distance: int = 0     # long-haul among distance-classified flights
    duration_long_haul_all: int = 0    # cross-check: long-haul-by-duration over ALL usable flights

    for departure_record in departures:
        dest_airport_code: str = departure_record.get("estArrivalAirport", "") or ""
        first_seen: int = departure_record.get("firstSeen", 0) or 0
        last_seen: int = departure_record.get("lastSeen", 0) or 0

        if not dest_airport_code or first_seen <= 0 or last_seen <= 0:
            continue

        usable_flights += 1

        # Duration cross-check tally over every usable flight (independent of coords).
        duration_minutes: float = (last_seen - first_seen) / 60.0
        is_long_by_duration: bool = duration_minutes >= LONG_HAUL_MIN_MINUTES
        if is_long_by_duration:
            duration_long_haul_all += 1

        # Resolve destination coords if possible.
        dest_airport: Optional[Dict[str, Any]] = airport_dict.get(dest_airport_code)
        distance_km: Optional[float] = None
        if (
            origin_lat is not None
            and dest_airport is not None
            and dest_airport.get("latitude_deg")
            and dest_airport.get("longitude_deg")
        ):
            try:
                dest_lat: float = float(dest_airport["latitude_deg"])
                dest_lon: float = float(dest_airport["longitude_deg"])
                distance_km = _great_circle_distance(origin_lat, origin_lon, dest_lat, dest_lon)
            except (ValueError, TypeError):
                distance_km = None

        if distance_km is not None:
            # Coords available -> classify by distance ONLY (no duration fallthrough).
            distance_classified += 1
            if distance_km >= LONG_HAUL_MIN_KM:
                long_haul_by_distance += 1
                long_haul_count += 1
        else:
            # No usable coords -> duration fallback.
            duration_classified += 1
            if is_long_by_duration:
                long_haul_count += 1

    long_haul_fraction: float = long_haul_count / usable_flights if usable_flights > 0 else 0.0
    distance_based_share: Optional[float] = (
        long_haul_by_distance / distance_classified if distance_classified > 0 else None
    )
    duration_based_share: Optional[float] = (
        duration_long_haul_all / usable_flights if usable_flights > 0 else None
    )

    breakdown: Dict[str, Any] = {
        "usable_flights": usable_flights,
        "long_haul_count": long_haul_count,
        "distance_classified": distance_classified,
        "duration_classified": duration_classified,
        "long_haul_by_distance": long_haul_by_distance,
        "long_haul_by_duration_fallback": long_haul_count - long_haul_by_distance,
        # Cross-check figures (scoring-and-kpis.md §4: report both when possible).
        "distance_based_share": distance_based_share,
        "duration_based_share": duration_based_share,
    }

    return long_haul_fraction, breakdown


def compute_growth(
    recent_flights: List[Dict[str, Any]],
    baseline_flights: List[Dict[str, Any]],
    window_days: int,
) -> tuple[Optional[float], bool]:
    """
    Compute growth rate between two windows.

    Growth = (recent_TrafficVolume - baseline_TrafficVolume) / baseline_TrafficVolume
    If baseline is empty, return (None, False) — drop Growth term from scoring.

    Args:
        recent_flights: Recent observation window flights.
        baseline_flights: Baseline window flights (BASELINE_LAG_DAYS earlier).
        window_days: Window size in days.

    Returns:
        Tuple of (growth_rate, is_valid) where:
        - growth_rate: Computed growth or None if baseline is empty
        - is_valid: True if both windows have data; False if baseline is empty
    """
    recent_volume: float = traffic_volume(recent_flights, window_days)
    baseline_volume: float = traffic_volume(baseline_flights, window_days)

    if baseline_volume <= 0:
        return None, False

    growth_rate: float = (recent_volume - baseline_volume) / baseline_volume
    return growth_rate, True


def hourly_clipping(
    departures: List[Dict[str, Any]],
    arrivals: List[Dict[str, Any]],
    hourly_cap: float,
    window_days: int = WINDOW_DAYS,
) -> float:
    """
    Compute fraction of operating hours where movements >= 0.9 × HourlyCapacity.

    The denominator is the total operating hours in the window
    (window_days × OPERATING_HOURS), NOT just the hours that happened to see
    traffic — an idle operating hour is an operating hour that did not clip and
    must stay in the denominator (scoring-and-kpis.md §6). Clamped to [0, 1].

    Args:
        departures: Departure flight records (bucketed by firstSeen).
        arrivals: Arrival flight records (bucketed by lastSeen).
        hourly_cap: HourlyCapacity for the airport.
        window_days: Number of days in the observation window.

    Returns:
        Fraction of operating hours at >= 90% capacity; 0.0 if hourly_cap <= 0.
    """
    if hourly_cap <= 0:
        return 0.0

    hourly_movements: Dict[int, int] = _hourly_movement_curve(departures, arrivals)
    if not hourly_movements:
        return 0.0

    clipping_threshold: float = 0.9 * hourly_cap
    clipped_hours: int = sum(1 for movements in hourly_movements.values() if movements >= clipping_threshold)
    total_operating_hours: int = max(1, window_days * OPERATING_HOURS)

    clipping_fraction: float = clipped_hours / total_operating_hours
    return min(1.0, clipping_fraction)


def _unmet_demand_kpi(utilization_val: float, growth_rate: Optional[float], hourly_clipping_val: float) -> float:
    """
    Compute unmet demand proxy (KPI helper; the public Q4 tool is unmet_demand).

    Formula: util_clamped × max(0, Growth) + HourlyClipping
    where util_clamped = min(Utilization, 1.0)

    Args:
        utilization_val: Utilization ratio.
        growth_rate: Growth rate or None if baseline empty.
        hourly_clipping_val: HourlyClipping fraction.

    Returns:
        UnmetDemand proxy value.
    """
    util_clamped: float = min(utilization_val, 1.0)
    growth_component: float = max(0.0, growth_rate) if growth_rate is not None else 0.0
    unmet_demand_val: float = util_clamped * growth_component + hourly_clipping_val
    return unmet_demand_val


def confidence_score(flights: List[Dict[str, Any]]) -> float:
    """
    Compute confidence as fraction of flights with valid airport estimates.

    Args:
        flights: Combined list of flights.

    Returns:
        Fraction in [0, 1]; 1.0 if all have non-null est*Airport, 0.0 if none do.
    """
    if not flights:
        return 0.0

    valid_flights: int = 0
    for flight_record in flights:
        has_dep_airport: bool = bool(flight_record.get("estDepartureAirport"))
        has_arr_airport: bool = bool(flight_record.get("estArrivalAirport"))
        if has_dep_airport and has_arr_airport:
            valid_flights += 1

    confidence_val: float = valid_flights / len(flights)
    return confidence_val


# ===== Output helpers (formatting / banding for the JSON tool layer) =====

def _r(value: Any, digits: int = 4) -> Any:
    """Round floats for clean JSON; pass ints, None, bools and other types through unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, digits)
    return value


def _recent_window() -> tuple[int, int]:
    """
    UTC-day-aligned recent observation window.

    A WINDOW_DAYS window ending STABLE_WINDOW_LAG_DAYS before today's UTC midnight.
    Aligning to midnight keeps the window (and therefore the on-disk OpenSky cache
    key) stable for the whole UTC day, so repeat queries and follow-ups reuse the
    cache instead of re-spending credits (data-and-apis.md §1.4).
    """
    now: datetime = datetime.now(timezone.utc)
    midnight: datetime = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end_unix: int = int(midnight.timestamp()) - STABLE_WINDOW_LAG_DAYS * 86400
    begin_unix: int = end_unix - WINDOW_DAYS * 86400
    return begin_unix, end_unix


def _baseline_window(recent_end: int) -> tuple[int, int]:
    """Growth baseline: a WINDOW_DAYS window ending BASELINE_LAG_DAYS before the recent window end."""
    end_unix: int = recent_end - BASELINE_LAG_DAYS * 86400
    begin_unix: int = end_unix - WINDOW_DAYS * 86400
    return begin_unix, end_unix


def _iso(unix_seconds: int) -> str:
    """Format a Unix timestamp as a UTC ISO-8601 string."""
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_descriptor(
    begin_unix: int, end_unix: int, baseline: Optional[tuple[int, int]] = None
) -> Dict[str, Any]:
    """Build the data-window block included in every tool's JSON output."""
    descriptor: Dict[str, Any] = {
        "window_days": WINDOW_DAYS,
        "stable_window_lag_days": STABLE_WINDOW_LAG_DAYS,
        "begin_unix": begin_unix,
        "end_unix": end_unix,
        "begin_utc": _iso(begin_unix),
        "end_utc": _iso(end_unix),
    }
    if baseline is not None:
        baseline_begin, baseline_end = baseline
        descriptor["baseline_window"] = {
            "baseline_lag_days": BASELINE_LAG_DAYS,
            "begin_unix": baseline_begin,
            "end_unix": baseline_end,
            "begin_utc": _iso(baseline_begin),
            "end_utc": _iso(baseline_end),
        }
    return descriptor


def _band_utilization(value: float) -> str:
    """Utilization band (scoring-and-kpis.md §3)."""
    if value < 0.50:
        return "Low"
    if value < 0.85:
        return "Moderate"
    if value <= 1.00:
        return "High"
    return "Over capacity"


def _band_peak_saturation(value: float) -> str:
    """PeakSaturation band (scoring-and-kpis.md §3)."""
    if value < 0.60:
        return "Comfortable"
    if value <= 0.90:
        return "Busy"
    return "Saturated"


def _band_long_haul(value: float) -> str:
    """LongHaulShare band (scoring-and-kpis.md §3)."""
    if value < 0.15:
        return "Mostly short-haul"
    if value <= 0.40:
        return "Mixed"
    return "Long-haul heavy"


def _band_unmet_demand(value: float) -> str:
    """UnmetDemand band (scoring-and-kpis.md §6)."""
    if value < 0.15:
        return "Low"
    if value <= 0.40:
        return "Moderate"
    return "High"


def _band_confidence(value: float) -> str:
    """Confidence band (scoring-and-kpis.md §7); < ~0.6 should downgrade certainty language."""
    if value >= 0.80:
        return "High"
    if value >= 0.60:
        return "Moderate"
    return "Low"


CONFIDENCE_CAVEAT: str = (
    "OpenSky counts are a crowdsourced ADS-B sample, not an official census; "
    "treat figures as indicative and weight low-confidence airports cautiously."
)


def _airport_identity(airport: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the resolved-identity block carried in every tool's JSON output."""
    return {
        "icao": airport.get("ident", ""),
        "iata": airport.get("iata_code", ""),
        "name": airport.get("name", ""),
        "municipality": airport.get("municipality", ""),
        "iso_region": airport.get("iso_region", ""),
    }


def _assumptions(keys: List[str]) -> Dict[str, Any]:
    """Return the subset of tunable constants relevant to a tool, for auditability."""
    all_constants: Dict[str, Any] = {
        "RUNWAY_THROUGHPUT_PER_HOUR": RUNWAY_THROUGHPUT_PER_HOUR,
        "OPERATING_HOURS": OPERATING_HOURS,
        "LONGRUNWAY_SHORT_FT": LONGRUNWAY_SHORT_FT,
        "LONGRUNWAY_HEAVY_FT": LONGRUNWAY_HEAVY_FT,
        "LENGTH_FACTOR": LENGTH_FACTOR,
        "LONG_HAUL_MIN_KM": LONG_HAUL_MIN_KM,
        "LONG_HAUL_MIN_MINUTES": LONG_HAUL_MIN_MINUTES,
        "WINDOW_DAYS": WINDOW_DAYS,
        "STABLE_WINDOW_LAG_DAYS": STABLE_WINDOW_LAG_DAYS,
        "BASELINE_LAG_DAYS": BASELINE_LAG_DAYS,
    }
    return {key: all_constants[key] for key in keys}


def _fetch_window(
    icao: str, begin_unix: int, end_unix: int
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """
    Fetch departures and arrivals for a window, collecting (not raising) fetch errors.

    Returning errors instead of raising lets a multi-airport tool (rank_region)
    degrade gracefully — one airport's network failure can't crash the ranking.
    """
    errors: List[str] = []
    try:
        departures: List[Dict[str, Any]] = get_flights(icao, begin_unix, end_unix, "departure")
    except requests.RequestException as exc:
        departures = []
        errors.append(f"{icao} departure fetch failed: {exc}")
    try:
        arrivals: List[Dict[str, Any]] = get_flights(icao, begin_unix, end_unix, "arrival")
    except requests.RequestException as exc:
        arrivals = []
        errors.append(f"{icao} arrival fetch failed: {exc}")
    return departures, arrivals, errors


def _kpis_from_flights(
    icao: str,
    departures: List[Dict[str, Any]],
    arrivals: List[Dict[str, Any]],
    recent_window: Tuple[int, int],
    growth: Optional[float] = None,
    growth_valid: bool = False,
    baseline_window: Optional[Tuple[int, int]] = None,
    errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Compute the full deterministic KPI set from already-fetched flight lists.

    Pure computation (no network) — shared by _airport_kpi_bundle (single-airport
    tools) and rank_region's concurrent flat-fetch path so both produce identical
    KPI dicts. Growth is passed in (computed by the caller) rather than fetched here.
    """
    all_flights: List[Dict[str, Any]] = departures + arrivals
    capacity: float = capacity_index(icao)
    hourly_cap: float = hourly_capacity(icao)
    traffic_vol: float = traffic_volume(all_flights, WINDOW_DAYS)
    peak: int = peak_load(departures, arrivals)
    share, share_breakdown = _long_haul_share_kpi(departures, icao)
    return {
        "icao": icao,
        "departures": len(departures),
        "arrivals": len(arrivals),
        "traffic_volume": traffic_vol,
        "peak_load": peak,
        "distinct_destinations": distinct_destinations(departures),
        "capacity_index": capacity,
        "hourly_capacity": hourly_cap,
        "utilization": utilization(traffic_vol, capacity),
        "peak_saturation": peak_saturation(peak, hourly_cap),
        "long_haul_share": share,
        "long_haul_breakdown": share_breakdown,
        "hourly_clipping": hourly_clipping(departures, arrivals, hourly_cap, WINDOW_DAYS),
        "growth": growth,
        "growth_valid": growth_valid,
        "confidence": confidence_score(all_flights),
        "recent_window": recent_window,
        "baseline_window": baseline_window,
        "errors": errors if errors is not None else [],
    }


def _airport_kpi_bundle(icao: str, with_growth: bool = False) -> Dict[str, Any]:
    """
    Compute the full deterministic KPI set for one airport over the standard recent window.

    Used by the single-airport tools. When with_growth is True a baseline window is
    also fetched (only if the recent window had flights) so Growth can be computed.
    """
    recent_begin, recent_end = _recent_window()
    departures, arrivals, errors = _fetch_window(icao, recent_begin, recent_end)

    growth: Optional[float] = None
    growth_valid: bool = False
    baseline: Optional[Tuple[int, int]] = None
    if with_growth and len(departures) + len(arrivals) > 0:
        baseline = _baseline_window(recent_end)
        baseline_dep, baseline_arr, baseline_errors = _fetch_window(icao, baseline[0], baseline[1])
        errors.extend(baseline_errors)
        growth, growth_valid = compute_growth(departures + arrivals, baseline_dep + baseline_arr, WINDOW_DAYS)

    return _kpis_from_flights(
        icao, departures, arrivals,
        recent_window=(recent_begin, recent_end),
        growth=growth, growth_valid=growth_valid,
        baseline_window=baseline, errors=errors,
    )


# ===== The four Q-tools (scoring-and-kpis.md §9) — each returns a strict JSON string =====

def rank_region(region_codes: Any) -> str:
    """
    Q1 — rank a region's commercial airports as terminal-expansion candidates.

    Computes each candidate's KPIs, min-max normalizes Utilization / Growth /
    PeakSaturation / LongHaulShare across the candidate set, and applies the §8
    weighted-sum ExpansionScore. Airports with no usable Growth have that term
    dropped, the remaining weights renormalized to 1, and confidence lowered.
    Returns a strict JSON string.
    """
    region_set: set[str] = set(region_codes)
    candidates: List[Dict[str, Any]] = region_airports(region_set)

    recent_begin, recent_end = _recent_window()
    baseline: tuple[int, int] = _baseline_window(recent_end)

    notes: List[str] = []

    def _aggregate(
        chunk_results: Dict[ChunkTask, List[Dict[str, Any]]], icao: str, chunks: List[Tuple[int, int]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        deps: List[Dict[str, Any]] = []
        arrs: List[Dict[str, Any]] = []
        for chunk_begin, chunk_end in chunks:
            deps.extend(chunk_results.get((icao, chunk_begin, chunk_end, "departure"), []))
            arrs.extend(chunk_results.get((icao, chunk_begin, chunk_end, "arrival"), []))
        return deps, arrs

    # ---- Pass 1: fetch every candidate's RECENT window in one concurrent batch ----
    recent_chunks: List[Tuple[int, int]] = _day_chunks(recent_begin, recent_end)
    recent_tasks: List[ChunkTask] = []
    for airport in candidates:
        for chunk_begin, chunk_end in recent_chunks:
            recent_tasks.append((airport["ident"], chunk_begin, chunk_end, "departure"))
            recent_tasks.append((airport["ident"], chunk_begin, chunk_end, "arrival"))
    recent_results, recent_errors = _fetch_chunks_concurrent(recent_tasks)
    notes.extend(recent_errors)

    bundles: List[Dict[str, Any]] = []
    for airport in candidates:
        deps, arrs = _aggregate(recent_results, airport["ident"], recent_chunks)
        bundle = _kpis_from_flights(
            airport["ident"], deps, arrs,
            recent_window=(recent_begin, recent_end), baseline_window=baseline,
        )
        bundle["airport"] = airport
        bundles.append(bundle)

    # ---- Pass 2: compute the 90-day Growth baseline ONLY for the top-K busiest candidates ----
    # Sparse regional fields can't yield a meaningful 90-day trend and won't top the ranking;
    # bounding Growth to the real contenders cuts ~half the API calls (scoring-and-kpis.md §5: credit-bounded).
    growth_candidates: List[Dict[str, Any]] = [
        b for b in sorted(bundles, key=lambda item: item["traffic_volume"], reverse=True)
        if b["traffic_volume"] > 0
    ][:RANK_GROWTH_TOP_K]
    baseline_chunks: List[Tuple[int, int]] = _day_chunks(baseline[0], baseline[1])
    baseline_tasks: List[ChunkTask] = []
    for bundle in growth_candidates:
        for chunk_begin, chunk_end in baseline_chunks:
            baseline_tasks.append((bundle["icao"], chunk_begin, chunk_end, "departure"))
            baseline_tasks.append((bundle["icao"], chunk_begin, chunk_end, "arrival"))
    if baseline_tasks:
        baseline_results, baseline_errors = _fetch_chunks_concurrent(baseline_tasks)
        notes.extend(baseline_errors)
        for bundle in growth_candidates:
            base_deps, base_arrs = _aggregate(baseline_results, bundle["icao"], baseline_chunks)
            baseline_volume: float = traffic_volume(base_deps + base_arrs, WINDOW_DAYS)
            if baseline_volume > 0:
                bundle["growth"] = (bundle["traffic_volume"] - baseline_volume) / baseline_volume
                bundle["growth_valid"] = True

    growth_computed_for: List[str] = [b["icao"] for b in growth_candidates]

    # Min-max ranges across the candidate set (Growth over non-null values only).
    util_values: List[float] = [b["utilization"] for b in bundles]
    peak_values: List[float] = [b["peak_saturation"] for b in bundles]
    lhs_values: List[float] = [b["long_haul_share"] for b in bundles]
    growth_values: List[float] = [b["growth"] for b in bundles if b["growth"] is not None]

    def _range(values: List[float]) -> tuple[float, float]:
        return (min(values), max(values)) if values else (0.0, 0.0)

    util_lo, util_hi = _range(util_values)
    peak_lo, peak_hi = _range(peak_values)
    lhs_lo, lhs_hi = _range(lhs_values)
    growth_lo, growth_hi = _range(growth_values)

    def _norm(value: Optional[float], low: float, high: float) -> Optional[float]:
        if value is None:
            return None
        if high <= low:
            return 0.0  # no spread within the set → equal contribution, ranking unaffected
        return (value - low) / (high - low)

    base_weights: Dict[str, float] = {
        "utilization": 0.40, "growth": 0.30, "peak_saturation": 0.20, "long_haul_share": 0.10,
    }

    ranked: List[Dict[str, Any]] = []
    for bundle in bundles:
        norm_util: Optional[float] = _norm(bundle["utilization"], util_lo, util_hi)
        norm_peak: Optional[float] = _norm(bundle["peak_saturation"], peak_lo, peak_hi)
        norm_lhs: Optional[float] = _norm(bundle["long_haul_share"], lhs_lo, lhs_hi)
        norm_growth: Optional[float] = _norm(bundle["growth"], growth_lo, growth_hi) if bundle["growth"] is not None else None

        if norm_growth is None:
            remaining: float = (
                base_weights["utilization"] + base_weights["peak_saturation"] + base_weights["long_haul_share"]
            )
            weights_used: Dict[str, float] = {
                "utilization": base_weights["utilization"] / remaining,
                "peak_saturation": base_weights["peak_saturation"] / remaining,
                "long_haul_share": base_weights["long_haul_share"] / remaining,
            }
            score: float = 100.0 * (
                weights_used["utilization"] * norm_util
                + weights_used["peak_saturation"] * norm_peak
                + weights_used["long_haul_share"] * norm_lhs
            )
            confidence_adj: float = bundle["confidence"] * CONFIDENCE_PENALTY_ON_GROWTH_NULL
            growth_note: Optional[str] = (
                "Growth unavailable (empty/sparse baseline); term dropped, weights renormalized, confidence lowered."
            )
        else:
            weights_used = dict(base_weights)
            score = 100.0 * (
                base_weights["utilization"] * norm_util
                + base_weights["growth"] * norm_growth
                + base_weights["peak_saturation"] * norm_peak
                + base_weights["long_haul_share"] * norm_lhs
            )
            confidence_adj = bundle["confidence"]
            growth_note = None

        ranked.append({
            "airport": _airport_identity(bundle["airport"]),
            "expansion_score": _r(score, 2),
            "kpis": {
                "traffic_volume": _r(bundle["traffic_volume"], 1),
                "utilization": _r(bundle["utilization"]),
                "utilization_band": _band_utilization(bundle["utilization"]),
                "growth": _r(bundle["growth"]),
                "peak_saturation": _r(bundle["peak_saturation"]),
                "peak_saturation_band": _band_peak_saturation(bundle["peak_saturation"]),
                "long_haul_share": _r(bundle["long_haul_share"]),
                "long_haul_band": _band_long_haul(bundle["long_haul_share"]),
            },
            "normalized": {
                "utilization": _r(norm_util),
                "growth": _r(norm_growth),
                "peak_saturation": _r(norm_peak),
                "long_haul_share": _r(norm_lhs),
            },
            "weights_used": {key: _r(val) for key, val in weights_used.items()},
            "confidence": _r(confidence_adj),
            "confidence_band": _band_confidence(confidence_adj),
            "movements_observed": bundle["departures"] + bundle["arrivals"],
            "growth_note": growth_note,
        })

    ranked.sort(key=lambda item: item["expansion_score"], reverse=True)
    for position, item in enumerate(ranked, start=1):
        item["rank"] = position

    output: Dict[str, Any] = {
        "question": "Q1 — regional ranking (terminal-expansion candidates)",
        "method": "Min-max normalize each KPI across the set, then weighted sum (scoring-and-kpis.md §8).",
        "region_codes": sorted(region_set),
        "candidate_count": len(candidates),
        "base_weights": base_weights,
        "set_has_growth": len(growth_values) > 0,
        "growth_computed_for": growth_computed_for,
        "growth_scope_note": (
            f"Growth (90-day trend) is computed only for the top {RANK_GROWTH_TOP_K} candidates by traffic; "
            "others have the Growth term dropped and remaining weights renormalized (scoring-and-kpis.md §5/§8)."
        ),
        "ranked": ranked,
        "data_window": _window_descriptor(recent_begin, recent_end, baseline),
        "assumptions": _assumptions([
            "RUNWAY_THROUGHPUT_PER_HOUR", "OPERATING_HOURS", "LONGRUNWAY_SHORT_FT",
            "LONGRUNWAY_HEAVY_FT", "LENGTH_FACTOR", "LONG_HAUL_MIN_KM",
            "LONG_HAUL_MIN_MINUTES", "WINDOW_DAYS", "STABLE_WINDOW_LAG_DAYS", "BASELINE_LAG_DAYS",
        ]),
        "confidence_caveat": CONFIDENCE_CAVEAT,
        "scope": "The normalized 0–100 score is meaningful only for ranking within this candidate set.",
        "notes": notes,
    }
    return json.dumps(output, indent=2)


def compare_airports(icao_list: List[str]) -> str:
    """
    Q2 — compare airports' congestion: TrafficVolume, Utilization, PeakSaturation per
    airport as absolute values + category bands (no normalized score, per §9/§8).
    Accepts ICAO codes, IATA codes, or names. Returns a strict JSON string.
    """
    recent_begin, recent_end = _recent_window()
    notes: List[str] = []
    items: List[Dict[str, Any]] = []
    for query in icao_list:
        airport: Dict[str, Any] = resolve_airport(query)
        bundle: Dict[str, Any] = _airport_kpi_bundle(airport["ident"], with_growth=False)
        if bundle["errors"]:
            notes.extend(bundle["errors"])
        items.append({
            "airport": _airport_identity(airport),
            "traffic_volume": _r(bundle["traffic_volume"], 1),
            "utilization": _r(bundle["utilization"]),
            "utilization_band": _band_utilization(bundle["utilization"]),
            "peak_load": bundle["peak_load"],
            "peak_saturation": _r(bundle["peak_saturation"]),
            "peak_saturation_band": _band_peak_saturation(bundle["peak_saturation"]),
            "capacity_index": _r(bundle["capacity_index"], 1),
            "hourly_capacity": _r(bundle["hourly_capacity"], 1),
            "movements_observed": bundle["departures"] + bundle["arrivals"],
            "confidence": _r(bundle["confidence"]),
            "confidence_band": _band_confidence(bundle["confidence"]),
        })

    output: Dict[str, Any] = {
        "question": "Q2 — pairwise congestion comparison",
        "airports": items,
        "data_window": _window_descriptor(recent_begin, recent_end),
        "assumptions": _assumptions([
            "RUNWAY_THROUGHPUT_PER_HOUR", "OPERATING_HOURS", "LONGRUNWAY_SHORT_FT",
            "LONGRUNWAY_HEAVY_FT", "LENGTH_FACTOR", "WINDOW_DAYS", "STABLE_WINDOW_LAG_DAYS",
        ]),
        "confidence_caveat": CONFIDENCE_CAVEAT,
        "scope": "Single-airport absolute values + category bands; no normalized score (no set to rank against).",
        "notes": notes,
    }
    return json.dumps(output, indent=2)


def long_haul_share(icao: str) -> str:
    """
    Q3 — long-haul share of departures (scoring-and-kpis.md §4): distance-based headline
    with a duration-based cross-check, plus a destination histogram. Accepts ICAO/IATA/
    name. Returns a strict JSON string.
    """
    airport: Dict[str, Any] = resolve_airport(icao)
    resolved_icao: str = airport["ident"]
    recent_begin, recent_end = _recent_window()
    departures, arrivals, errors = _fetch_window(resolved_icao, recent_begin, recent_end)

    share, breakdown = _long_haul_share_kpi(departures, resolved_icao)

    # Destination histogram: top destinations by departure count.
    airports_index: Dict[str, Dict[str, Any]] = {a["ident"]: a for a in _load_airports()}
    histogram: Dict[str, int] = {}
    for departure_record in departures:
        destination: str = departure_record.get("estArrivalAirport") or ""
        if destination:
            histogram[destination] = histogram.get(destination, 0) + 1
    top_destinations = sorted(histogram.items(), key=lambda kv: kv[1], reverse=True)[:10]
    destination_histogram: List[Dict[str, Any]] = [
        {"icao": dest, "name": airports_index.get(dest, {}).get("name", ""), "flights": count}
        for dest, count in top_destinations
    ]

    # Confidence over departures — the set this metric is computed on.
    confidence: float = confidence_score(departures)

    output: Dict[str, Any] = {
        "question": "Q3 — long-haul share of departures",
        "airport": _airport_identity(airport),
        "long_haul_share": _r(share),
        "long_haul_band": _band_long_haul(share),
        "distance_based_share": _r(breakdown["distance_based_share"]),
        "duration_based_share": _r(breakdown["duration_based_share"]),
        "breakdown": breakdown,
        "destination_histogram": destination_histogram,
        "departures_observed": len(departures),
        "confidence": _r(confidence),
        "confidence_band": _band_confidence(confidence),
        "data_window": _window_descriptor(recent_begin, recent_end),
        "assumptions": _assumptions([
            "LONG_HAUL_MIN_KM", "LONG_HAUL_MIN_MINUTES", "WINDOW_DAYS", "STABLE_WINDOW_LAG_DAYS",
        ]),
        "confidence_caveat": CONFIDENCE_CAVEAT,
        "scope": (
            "Distance-based classification is the headline; duration is a fallback only when "
            "destination coordinates are unavailable. Single-airport metric — absolute share + band."
        ),
        "notes": errors,
    }
    return json.dumps(output, indent=2)


def unmet_demand(icao: str) -> str:
    """
    Q4 — unmet-demand proxy (scoring-and-kpis.md §6): UnmetDemand with its HourlyClipping,
    Utilization, and Growth components, plus an explicit proxy interpretation. Accepts
    ICAO/IATA/name. Returns a strict JSON string.
    """
    airport: Dict[str, Any] = resolve_airport(icao)
    bundle: Dict[str, Any] = _airport_kpi_bundle(airport["ident"], with_growth=True)

    proxy: float = _unmet_demand_kpi(bundle["utilization"], bundle["growth"], bundle["hourly_clipping"])
    confidence: float = bundle["confidence"]
    growth_note: Optional[str] = None
    if not bundle["growth_valid"]:
        confidence = confidence * CONFIDENCE_PENALTY_ON_GROWTH_NULL
        growth_note = "Growth baseline empty/unavailable; growth treated as 0 in the proxy and confidence lowered."

    util_clamped: float = min(bundle["utilization"], 1.0)
    output: Dict[str, Any] = {
        "question": "Q4 — unmet flight demand (proxy) and why",
        "airport": _airport_identity(airport),
        "unmet_demand": _r(proxy),
        "unmet_demand_band": _band_unmet_demand(proxy),
        "components": {
            "utilization": _r(bundle["utilization"]),
            "utilization_band": _band_utilization(bundle["utilization"]),
            "utilization_clamped": _r(util_clamped),
            "growth": _r(bundle["growth"]),
            "growth_valid": bundle["growth_valid"],
            "hourly_clipping": _r(bundle["hourly_clipping"]),
            "peak_saturation": _r(bundle["peak_saturation"]),
            "peak_saturation_band": _band_peak_saturation(bundle["peak_saturation"]),
        },
        "formula": "UnmetDemand = min(Utilization, 1.0) * max(0, Growth) + HourlyClipping",
        "interpretation": (
            "Sustained hourly clipping plus positive growth at high utilization indicates demand is being "
            "capped by capacity — an expansion candidate. This is an explicit proxy, not a measurement of "
            "unmet demand (no public source measures enplanements or turned-away flights)."
        ),
        "confidence": _r(confidence),
        "confidence_band": _band_confidence(confidence),
        "growth_note": growth_note,
        "data_window": _window_descriptor(
            bundle["recent_window"][0], bundle["recent_window"][1], bundle["baseline_window"]
        ),
        "assumptions": _assumptions([
            "RUNWAY_THROUGHPUT_PER_HOUR", "OPERATING_HOURS", "LONGRUNWAY_SHORT_FT",
            "LONGRUNWAY_HEAVY_FT", "LENGTH_FACTOR", "WINDOW_DAYS", "STABLE_WINDOW_LAG_DAYS", "BASELINE_LAG_DAYS",
        ]),
        "confidence_caveat": CONFIDENCE_CAVEAT,
        "scope": "Single-airport proxy — absolute value + band; not normalized.",
        "notes": bundle["errors"],
    }
    return json.dumps(output, indent=2)


if __name__ == "__main__":
    print("\n########## Q1: rank_region — New England ##########")
    print(rank_region(NEW_ENGLAND_REGIONS))

    print("\n########## Q2: compare_airports — LA vs Santa Ana ##########")
    print(compare_airports(["LA", "Santa Ana"]))

    print("\n########## Q3: long_haul_share — Anchorage ##########")
    print(long_haul_share("Anchorage"))

    print("\n########## Q4: unmet_demand — SFO ##########")
    print(unmet_demand("SFO"))
