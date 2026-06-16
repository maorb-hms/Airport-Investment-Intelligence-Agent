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


# Constants
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

# OpenSky Network API constants (data-and-apis.md §1)
OPENSKY_AUTH_URL: str = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_API_BASE: str = "https://opensky-network.org/api"
OPENSKY_CACHE_DIR: Path = CACHE_DIR / "opensky"
OPENSKY_TOKEN_EXPIRY_BUFFER_SEC: int = 60  # Refresh token 60s before actual expiry
STABLE_WINDOW_LAG_DAYS: int = 2  # Query windows must end at least this far in the past (data-and-apis.md §1.4)
WINDOW_DAYS: int = 7  # KPI observation window length (scoring-and-kpis.md §0)
MAX_API_INTERVAL_DAYS: int = 1  # OpenSky /flights/* hard limit per call (data-and-apis.md §1.2)

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

    # Check metro aliases first (data-and-apis.md §2.1)
    if query_trimmed in METRO_ALIASES:
        target_icao: str = METRO_ALIASES[query_trimmed]
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


if __name__ == "__main__":
    import pprint

    print("\n=== resolve_airport('Anchorage') ===")
    try:
        anchorage_result: Dict[str, Any] = resolve_airport("Anchorage")
        pprint.pprint(anchorage_result)
    except Exception as e:
        print(f"ERROR: {e}")

    print("\n=== region_airports(NEW_ENGLAND_REGIONS) ===")
    try:
        ne_airports: List[Dict[str, Any]] = region_airports(NEW_ENGLAND_REGIONS)
        print(f"Found {len(ne_airports)} New England candidate airports:")
        for airport_record in ne_airports:
            airport_icao: str = airport_record["ident"]
            airport_name: str = airport_record["name"]
            airport_municipality: str = airport_record["municipality"]
            print(f"  {airport_icao:6s} {airport_name:50s} ({airport_municipality})")
    except Exception as e:
        print(f"ERROR: {e}")

    print("\n=== runway_capacity('KSFO') ===")
    try:
        sfo_capacity: Dict[str, Any] = runway_capacity("KSFO")
        pprint.pprint(sfo_capacity)
    except Exception as e:
        print(f"ERROR: {e}")

    print("\n=== get_flights('KSFO', 1-day window ending STABLE_WINDOW_LAG_DAYS ago) ===")
    try:
        now_unix: int = int(datetime.now().timestamp())
        window_end_unix: int = now_unix - (STABLE_WINDOW_LAG_DAYS * 86400)
        window_begin_unix: int = window_end_unix - (MAX_API_INTERVAL_DAYS * 86400)

        flights: List[Dict[str, Any]] = get_flights("KSFO", window_begin_unix, window_end_unix, "departure")
        print(f"Fetched {len(flights)} KSFO departures")

        if flights:
            sample_flight: Dict[str, Any] = flights[0]
            print("\nSample flight record:")
            pprint.pprint(sample_flight)
        else:
            print("No flights returned (may be outside operating hours or network coverage)")

    except Exception as e:
        print(f"ERROR: {e}")
