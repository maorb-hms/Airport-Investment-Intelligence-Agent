"""Deterministic Layer (pure Python SSOT): data ingestion + all KPI math, returns strict JSON; no AI/Anthropic deps (architecture.md §3)."""

import os
import json
import csv
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
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

    # Get token and make request
    access_token: str = _get_oauth_token()
    auth_headers: Dict[str, str] = {"Authorization": f"Bearer {access_token}"}

    api_response: requests.Response = requests.get(
        api_endpoint, params=api_params, headers=auth_headers, timeout=30
    )

    # Handle 401 by refreshing token and retrying
    if api_response.status_code == 401:
        global _cached_oauth_token, _token_expiry_time
        _cached_oauth_token = None
        _token_expiry_time = None
        access_token = _get_oauth_token()
        auth_headers = {"Authorization": f"Bearer {access_token}"}
        api_response = requests.get(
            api_endpoint, params=api_params, headers=auth_headers, timeout=30
        )

    api_response.raise_for_status()

    # Parse response (array of flight objects or empty array)
    flights: List[Dict[str, Any]] = api_response.json()
    if not isinstance(flights, list):
        flights = []

    # Cache on disk for future requests
    with open(cache_file_path, "w", encoding="utf-8") as f:
        json.dump(flights, f)

    return flights


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

    seconds_per_day: int = 86400
    all_flights: List[Dict[str, Any]] = []

    # Chunk into 1-day windows and fetch each
    current_begin: int = begin_unix
    while current_begin < end_unix:
        current_end: int = min(current_begin + seconds_per_day, end_unix)
        day_flights: List[Dict[str, Any]] = _get_opensky_flights(icao, current_begin, current_end, kind)
        all_flights.extend(day_flights)
        current_begin = current_end

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


def long_haul_share(departures: List[Dict[str, Any]], origin_icao: str) -> tuple[float, Dict[str, Any]]:
    """
    Compute fraction of long-haul departures.

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


def unmet_demand(utilization_val: float, growth_rate: Optional[float], hourly_clipping_val: float) -> float:
    """
    Compute unmet demand proxy.

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


if __name__ == "__main__":
    import pprint

    print("\n" + "=" * 70)
    print("KPI COMPUTATION TEST: KLAX (Los Angeles International)")
    print("=" * 70)

    # Resolve the airport
    print("\n[1] Resolving airport...")
    try:
        klax_airport: Dict[str, Any] = resolve_airport("LA")
        print(f"    ICAO: {klax_airport['ident']}")
        print(f"    Name: {klax_airport['name']}")
        print(f"    Region: {klax_airport['iso_region']}")
    except Exception as e:
        print(f"    ERROR resolving airport: {e}")
        exit(1)

    klax_icao: str = klax_airport["ident"]

    # Get runway capacity
    print("\n[2] Computing runway capacity...")
    try:
        klax_runway_cap: Dict[str, Any] = runway_capacity(klax_icao)
        print(f"    Usable runways: {klax_runway_cap['usable_runway_count']}")
        print(f"    Longest runway: {klax_runway_cap['longest_ft']} ft")
    except Exception as e:
        print(f"    ERROR: {e}")
        exit(1)

    # Fetch flights (1-day recent window)
    print(f"\n[3] Fetching flights (1-day recent window ending {STABLE_WINDOW_LAG_DAYS} days ago)...")
    print("    (Requires OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET in .env)")
    try:
        now_unix: int = int(datetime.now().timestamp())
        window_end_unix: int = now_unix - (STABLE_WINDOW_LAG_DAYS * 86400)
        window_begin_unix: int = window_end_unix - (MAX_API_INTERVAL_DAYS * 86400)

        departures: List[Dict[str, Any]] = get_flights(klax_icao, window_begin_unix, window_end_unix, "departure")
        arrivals: List[Dict[str, Any]] = get_flights(klax_icao, window_begin_unix, window_end_unix, "arrival")
        all_flights: List[Dict[str, Any]] = departures + arrivals

        print(f"    Departures: {len(departures)}")
        print(f"    Arrivals: {len(arrivals)}")
        print(f"    Total movements: {len(all_flights)}")

    except (ValueError, requests.exceptions.RequestException) as e:
        print(f"    ERROR (network/credentials): {e}")
        print("    Skipping live data; using mock data for KPI demonstration...")
        mock_dep: Dict[str, Any] = {
            "firstSeen": window_begin_unix + 10000,
            "lastSeen": window_begin_unix + 20000,
            "estDepartureAirport": "KLAX",
            "estArrivalAirport": "KORD",
        }
        departures = [mock_dep] * 100
        arrivals = [mock_dep] * 80
        all_flights = departures + arrivals

    # Compute KPIs
    print("\n[4] Computing KPIs...")
    tv: float = traffic_volume(all_flights, 1)
    pl: int = peak_load(departures, arrivals)
    dd: int = distinct_destinations(departures)
    ci: float = capacity_index(klax_icao)
    hc: float = hourly_capacity(klax_icao)
    util: float = utilization(tv, ci)
    ps: float = peak_saturation(pl, hc)
    lhs: float
    lhs_breakdown: Dict[str, Any]
    lhs, lhs_breakdown = long_haul_share(departures, klax_icao)
    hc_val: float = hourly_clipping(departures, arrivals, hc, window_days=1)
    ud: float = unmet_demand(util, None, hc_val)
    conf: float = confidence_score(all_flights)

    # Display results
    print("\n" + "=" * 70)
    print("KPI RESULTS FOR KLAX")
    print("=" * 70)
    print(f"\nDemand KPIs (OpenSky window):")
    print(f"  TrafficVolume:           {tv:10.2f} movements/day")
    print(f"  PeakLoad:                {pl:10d} movements/hour (busiest hour)")
    print(f"  DistinctDestinations:    {dd:10d} unique destination airports")

    print(f"\nCapacity KPIs (OurAirports runways):")
    print(f"  CapacityIndex:           {ci:10.2f} max movements/day")
    print(f"  HourlyCapacity:          {hc:10.2f} max movements/hour")

    print(f"\nCongestion KPIs:")
    print(f"  Utilization:             {util:10.4f} ({util*100:6.2f}%)")
    print(f"  PeakSaturation:          {ps:10.4f} ({ps*100:6.2f}%)")
    print(f"  HourlyClipping (>90%):   {hc_val:10.4f} ({hc_val*100:6.2f}%)")

    print(f"\nLong-haul & Growth:")
    print(f"  LongHaulShare:           {lhs:10.4f} ({lhs*100:6.2f}%)")
    print(f"    - usable flights:      {lhs_breakdown['usable_flights']:6d}")
    print(f"    - long-haul (headline):{lhs_breakdown['long_haul_count']:6d} flights")
    print(f"    - by distance:         {lhs_breakdown['long_haul_by_distance']:6d} (coords available)")
    print(f"    - by duration fallback:{lhs_breakdown['long_haul_by_duration_fallback']:6d} (no coords)")
    print(f"  Growth (vs. 90d ago):    (null baseline in 1-day demo)")

    print(f"\nProxies & Confidence:")
    print(f"  UnmetDemand:             {ud:10.4f}")
    print(f"  Confidence:              {conf:10.4f} ({conf*100:6.2f}%)")

    print(f"\nTunable Constants (auditable assumptions):")
    print(f"  RUNWAY_THROUGHPUT_PER_HOUR: {RUNWAY_THROUGHPUT_PER_HOUR}")
    print(f"  OPERATING_HOURS:            {OPERATING_HOURS}")
    print(f"  LONG_HAUL_MIN_KM:           {LONG_HAUL_MIN_KM}")
    print(f"  BASELINE_LAG_DAYS:          {BASELINE_LAG_DAYS}")
    print(f"  WINDOW_DAYS:                {WINDOW_DAYS}")

    print("\n" + "=" * 70)
    print("Reference: data-and-apis.md §1.5 baseline for LAX 1-day window = ~1,553 movements")
    print("=" * 70 + "\n")
