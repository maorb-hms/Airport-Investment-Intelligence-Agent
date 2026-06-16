# Scoring & KPIs — Deterministic Algorithms

Every number is computed by code in the Deterministic Layer (`test_tool.py`, see [`architecture.md`](architecture.md)) and returned as **strict JSON**. The LLM never produces a number. The raw fields these formulas consume are defined in [`data-and-apis.md`](data-and-apis.md).

Every constant below is a **stated, tunable assumption** and must be echoed in the JSON output so answers are auditable.

---

## 0. Tunable constants (assumptions)
```
RUNWAY_THROUGHPUT_PER_HOUR = 30      # movements/hr for one runway (typical single-runway capacity)
OPERATING_HOURS            = 18      # active hours/day
LONGRUNWAY_SHORT_FT        = 6000    # below → small-aircraft only
LONGRUNWAY_HEAVY_FT        = 9000    # above → efficiently handles heavies
LENGTH_FACTOR              = {short: 0.7, normal: 1.0, heavy: 1.1}
LONG_HAUL_MIN_KM           = 4000    # great-circle distance threshold for "long-haul"
LONG_HAUL_MIN_MINUTES      = 360     # duration fallback when coords unavailable
STABLE_WINDOW_LAG_DAYS     = 2       # query windows must end at least this far in the past
```

---

## 1. Demand KPIs (from OpenSky)
- `TrafficVolume = (count(departures) + count(arrivals)) / window_days` — average daily movements.
- `PeakLoad = max over hour-buckets of movements_in_hour` — congestion is felt at peaks, not daily averages.
- `DistinctDestinations = |set(estArrivalAirport for departures)|` — connectivity / hub-ness.

## 2. Capacity KPI (from OurAirports `runways.csv`)
```
usable_runways = runways where closed == 0
longest_ft     = max(length_ft of usable_runways)
length_factor  = LENGTH_FACTOR[ short  if longest_ft <  LONGRUNWAY_SHORT_FT
                                heavy  if longest_ft >= LONGRUNWAY_HEAVY_FT
                                normal otherwise ]
CapacityIndex  = count(usable_runways) * RUNWAY_THROUGHPUT_PER_HOUR * OPERATING_HOURS * length_factor   # max movements/day
HourlyCapacity = count(usable_runways) * RUNWAY_THROUGHPUT_PER_HOUR * length_factor
```

## 3. Congestion / Utilization KPIs  → answers Q2
```
Utilization    = TrafficVolume / CapacityIndex     # 0..1+ ; >1 means over nominal capacity
PeakSaturation = PeakLoad / HourlyCapacity         # how close peaks run to the ceiling
```

## 4. Long-haul KPI  → answers Q3
For each departure, classify haul:
```
if both airports resolve to coords:
    long_haul = great_circle_km(origin, dest) >= LONG_HAUL_MIN_KM
else:
    long_haul = (lastSeen - firstSeen) / 60 >= LONG_HAUL_MIN_MINUTES   # duration fallback
LongHaulShare = count(long_haul flights) / count(flights with a usable signal)
```
Report both distance-based and duration-based figures when possible (cross-check).

## 5. Growth KPI (trend)
Pull two OpenSky windows (recent vs. a baseline months earlier; both cached):
```
Growth = (recent_TrafficVolume - baseline_TrafficVolume) / baseline_TrafficVolume
```
**Assumption:** limited to airports referenced in a query (credit-bounded), precomputed/cached — not a live scan of all US airports.

## 6. Unmet-demand proxy  → answers Q4
No source measures unmet demand → explicit proxy:
```
HourlyClipping = fraction of operating hours where movements_in_hour >= 0.9 * HourlyCapacity
UnmetDemand    = norm(Utilization) * max(0, Growth) + HourlyClipping
```
**Interpretation given to the user:** sustained hourly clipping + positive growth at high utilization ⇒ demand is being capped by capacity ⇒ expansion candidate. Always labelled a **proxy**.

## 7. Confidence / uncertainty KPI (attached to EVERY answer)
```
Confidence = count(flights with non-null est*Airport) / count(all flights)
```
Reported as a 0–1 band plus a one-line caveat (sample vs. census). Low confidence (< ~0.6) must **downgrade the certainty language** in the explanation.

## 8. Composite Expansion Score  → answers Q1 (ranking)
For a candidate set (e.g. all New England commercial airports), min-max normalize each KPI **across the set**, then weighted sum:
```
norm(x) = (x - min_set) / (max_set - min_set)        # 0..1 within the candidate set
ExpansionScore = 100 * ( 0.40 * norm(Utilization)
                       + 0.30 * norm(Growth)
                       + 0.20 * norm(PeakSaturation)
                       + 0.10 * norm(LongHaulShare) )
```
Rank descending. Weights are stated assumptions (high current utilization + rising demand ⇒ strongest expansion ROI).

> **Single-airport questions (Q2/Q3/Q4)** have no set to normalize against → report **absolute KPI values + category bands**, and say so. The normalized 0–100 score is only meaningful for ranking a set (Q1).

---

## 9. Question → tool mapping (the routing contract)
| Question | Tool (`test_tool.py`) | Core KPIs returned |
|---|---|---|
| Q1 — regional ranking | `rank_region(region_codes)` | `ExpansionScore` + full KPI breakdown per airport |
| Q2 — pairwise comparison | `compare_airports([icao, ...])` | `Utilization`, `PeakSaturation`, `TrafficVolume` per airport |
| Q3 — long-haul metric | `long_haul_share(icao)` | `LongHaulShare` (distance + duration) + destination histogram |
| Q4 — unmet demand | `unmet_demand(icao)` | `UnmetDemand`, `HourlyClipping`, `Utilization`, `Growth` |

**Every tool result also carries:** resolved airport identity (name/ICAO/IATA), `Confidence`, the assumption constants used, and the data window. This is the JSON the Routing Layer translates into natural language.
