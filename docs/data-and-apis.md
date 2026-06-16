# Data Sources & APIs

How the agent gathers data. Two sources only: **OpenSky** (dynamic demand engine) and **OurAirports** (static reference). They are combined in the Deterministic Layer (`test_tool.py`) — see [`architecture.md`](architecture.md). The KPI math that consumes these fields lives in [`scoring-and-kpis.md`](scoring-and-kpis.md).

---

## 1. Dynamic Data — OpenSky Network REST API (the demand engine)
All flight-movement signals come from OpenSky. Credentials are supplied via `.env` and **must never be hardcoded or committed**:
```
OPENSKY_CLIENT_ID=...
OPENSKY_CLIENT_SECRET=...
```

### 1.1 Authentication — OAuth2 client-credentials
- **Token endpoint** (POST, `application/x-www-form-urlencoded`):
  `https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token`
- **Body:** `grant_type=client_credentials`, `client_id`, `client_secret`.
- **Returns:** `access_token`, used as a `Bearer` header on every API call.
- **Expiry:** `expires_in = 1800s` (30 min). The client must **cache the token and refresh on expiry or on a `401`**.

### 1.2 API base & endpoints
**Base:** `https://opensky-network.org/api`

| Endpoint | Params | Returns |
|---|---|---|
| `GET /flights/departure` | `airport` (ICAO), `begin` (unix s), `end` (unix s) | array of flights that departed the airport in the window |
| `GET /flights/arrival` | `airport` (ICAO), `begin` (unix s), `end` (unix s) | array of flights that arrived at the airport in the window |

`begin`/`end` are Unix epoch seconds. Compute them from UTC.

**VERIFIED interval limit:** a single `/flights/*` call must cover **≤ 1 day** — intervals of 2 days or more return **HTTP 400**. To build a `WINDOW_DAYS=7` observation window, `get_flights` must **fetch 7 consecutive 1-day calls and aggregate** (≈7 credits per airport per kind). Historical depth is ample (1-day windows at 30/90/180 days ago all return data), so the 90-day Growth baseline is retrievable.

### 1.3 Flight record fields (verified live)
| Field | Used for |
|---|---|
| `icao24` | distinct-aircraft counts |
| `firstSeen`, `lastSeen` (unix s) | **duration = lastSeen − firstSeen** → haul-length proxy; `firstSeen` hour bucket → hourly load curve |
| `estDepartureAirport`, `estArrivalAirport` (ICAO) | route / destination set |
| `estDepartureAirportHorizDistance`, `estArrivalAirportHorizDistance` (m) | match quality |
| `departureAirportCandidatesCount`, `arrivalAirportCandidatesCount` | **confidence indicator** |
| `callsign` | display / debugging |

### 1.4 Constraints & assumptions to encode
- **Batch finalization:** movement data is finalized nightly → **always query windows that end ≥ ~2 days in the past** (`STABLE_WINDOW_LAG_DAYS`) for stable counts.
- **Credit budget:** ~4,000/day authenticated; one airport-window ≈ one call. **Cache every OpenSky response on disk** (key: airport + window + kind) so follow-ups and growth comparisons don't re-spend credits.
- **Coverage:** crowdsourced ADS-B → strong at hubs, undercounts small fields. Counts are a **sample, not a census** → drives the Confidence KPI in [`scoring-and-kpis.md`](scoring-and-kpis.md).

### 1.5 Reference behaviour observed in testing (sanity baselines)
- 1-day window movements (dep+arr): LAX ≈ 1,553 · SFO ≈ 1,165 · SNA ≈ 499.
- Anchorage (`PANC`) departures: ~82% carry a usable `estArrivalAirport`; duration cleanly separates haul length (Asia/transcon ≫ intra-Alaska hops).

---

## 2. Static Reference — OurAirports CSVs (identity + capacity)
Pure reference data (airport identity, geography, runways). Carries **no demand information** → no analytical staleness. **Fetched at runtime from the canonical URLs, cached locally, re-fetched if the cache is older than N days** (default 7). No scraping, no API key.

| File | Canonical URL |
|---|---|
| `airports.csv` | `https://davidmegginson.github.io/ourairports-data/airports.csv` |
| `runways.csv` | `https://davidmegginson.github.io/ourairports-data/runways.csv` |

### 2.1 `airports.csv` — columns used
`ident` (ICAO), `type`, `name`, `latitude_deg`, `longitude_deg`, `iso_country`, `iso_region`, `municipality`, `scheduled_service`, `iata_code`.

**Roles:**
- `ident` / `iata_code` / `name` / `municipality` → resolve names ↔ ICAO (e.g. "Anchorage" → `PANC`) and produce human-readable explanations.
- `iso_region` → region membership. **New England = `US-ME, US-NH, US-VT, US-MA, US-RI, US-CT`.**
- `latitude_deg` / `longitude_deg` → great-circle distance for precise long-haul classification.
- `type` + `scheduled_service` → **candidate universe** = `type in {large_airport, medium_airport}` AND `scheduled_service == "yes"`; heliports, closed strips, and small_airport are excluded (this filter is tunable).

**Metro disambiguation rule (`resolve_airport`):** a metro name maps to multiple airports. Resolve a bare metro/city to its **primary commercial airport** (highest `type`/traffic) by default, state that assumption in the answer, and offer the alternatives. Required aliases for the canonical questions:
- `"LA"` / `"Los Angeles"` → `KLAX` (alternatives: `KBUR`, `KLGB`, `KONT`)
- `"Santa Ana"` → `KSNA`
- `"Anchorage"` → `PANC`
- `"SFO"` / `"San Francisco"` → `KSFO`

### 2.2 `runways.csv` — columns used
`airport_ident`, `length_ft`, `width_ft`, `lighted`, `closed`.

**Role:** the **CapacityIndex** — count of non-closed runways and longest runway length feed the capacity formula in [`scoring-and-kpis.md`](scoring-and-kpis.md).

---

## 3. How the two combine
- **OpenSky = the numerator** of nearly every KPI (movements, peaks, haul mix, growth, routes).
- **OurAirports = the denominator + the filter** (runway capacity; region/coords/identity).
- The deterministic score is a transparent weighted sum defensible line-by-line — see [`scoring-and-kpis.md`](scoring-and-kpis.md).
