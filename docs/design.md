# Product Design — Airport Investment Intelligence Agent

> **Reading map:** this file is the overview. Detail lives in:
> - [`architecture.md`](architecture.md) — the strict 3-tier code structure.
> - [`data-and-apis.md`](data-and-apis.md) — the data sources and how to communicate with them.
> - [`scoring-and-kpis.md`](scoring-and-kpis.md) — the deterministic KPI formulas and scoring model.

## 1. Objective
An interactive AI Agent for a firm that invests in **US airport modernization / terminal-expansion** projects. It helps analysts find airports where renovation will be most profitable, judged by **increased flight (and proxied passenger) demand vs. capacity**.

The agent must:
- Gather airport activity data from a public aviation API.
- **Rank or compare** airports with a **deterministic, code-computed score** — never LLM-generated numbers.
- **Explain its reasoning**, including the KPI breakdown behind every score.
- Support **conversational follow-ups** (session state).
- **Explicitly state assumptions, uncertainty, and scope** on every answer.

## 2. Canonical questions the agent must answer
| # | Question | Type |
|---|---|---|
| Q1 | Which airports in New England are strong candidates for terminal expansion? | regional **ranking** |
| Q2 | Compare LA and Santa Ana airport congestion levels. | pairwise **comparison** |
| Q3 | What % of flights out of Anchorage are long-haul? | single-airport **metric** |
| Q4 | What is the unmet flight demand at SFO, and why? | single-airport **proxy + explanation** |

See [`scoring-and-kpis.md`](scoring-and-kpis.md) §"Question → tool mapping" for which tool/KPIs each question routes to.

## 3. Scope & non-goals (must be communicated to the user)
- **No real passenger counts.** The data API exposes *flight movements*, not enplanements → passenger demand is **proxied by movements** (stated assumption, not a measurement).
- **Capacity is proxied from runway infrastructure**, not gates/terminals (no free source exposes those).
- **US airports only.**
- **Not a real-time monitor** — movement data is batch-finalized ~1 day in arrears.

## 4. Data architecture (one dynamic API + one static reference)
Combined deterministically to avoid hallucination. Full details in [`data-and-apis.md`](data-and-apis.md).
- **Dynamic — OpenSky Network REST API:** the demand engine (flight movements, routes, durations). OAuth2; credentials via `.env`.
- **Static — OurAirports CSVs** (`airports.csv`, `runways.csv`): identity, geography, and runway-based capacity. Fetched at runtime from canonical URLs and cached locally → no analytical staleness, no scraping, no key.

There is **no passenger/capacity API** — that gap is handled by proxies (§3).

## 5. Build milestones (commit after each verified step)
Build them one at a time, committing after each verified step per [`architecture.md`](architecture.md) Version Control Rule.

1. **Scaffold** — venv, `requirements.txt` (`streamlit`, `anthropic`, `requests`, `pandas`, `python-dotenv`; **pin versions**), `.gitignore` (`.env`, `__pycache__`, data cache), `.env`, git init + first commit.

> **Model choice:** default to **`claude-sonnet-4-6`** for the routing/explanation layer (fast, capable, cost-effective); use **`claude-opus-4-8`** if you want maximum reasoning quality. Pin the model id as a constant in `agent.py`.
2. **Reference loader** (deterministic) — OurAirports runtime-refresh + cache; `resolve_airport`, `region_airports`, `runway_capacity`.
3. **OpenSky client** (deterministic) — token caching + `get_flights` with on-disk response cache.
4. **KPI functions** (deterministic) — the formulas in [`scoring-and-kpis.md`](scoring-and-kpis.md).
5. **Tool functions** (deterministic) — `rank_region`, `compare_airports`, `long_haul_share`, `unmet_demand`, each returning strict JSON.
6. **Routing layer** (`agent.py`) — Anthropic system prompt + tool schemas; intent → tool → JSON.
7. **Explanation + uncertainty** (`agent.py`) — JSON → natural language; surface assumptions/confidence/scope; follow-ups.
8. **Chat UI** (`app.py`) — Streamlit + `st.session_state`; verify the 4 canonical questions end-to-end.
9. **(Bonus)** voice input; README; final design notes.

## 6. Design-doc deliverable (the writeup)
- **Scoring methodology** → [`scoring-and-kpis.md`](scoring-and-kpis.md).
- **Key tradeoffs** → OpenSky-only simplicity vs. no passenger/capacity data; movement-as-demand proxy; ADS-B sampling; credit-bounded growth.
- **Where/how AI is used** → strictly intent parsing, airport resolution, tool selection, and explaining deterministic JSON — never numeric computation.
