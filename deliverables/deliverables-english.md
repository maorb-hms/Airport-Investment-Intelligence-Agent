# Deliverables — Airport Investment Intelligence Agent

This document maps the assignment's required deliverables to where each one lives, and answers the three design questions directly.

> **The assignment asked for:** source code, plus a short design/architecture document explaining (1) scoring methodology, (2) key tradeoffs, and (3) where/how AI is used.

---

## 1. Source Code

The full source is in this repository:

| File | Layer | Responsibility |
|---|---|---|
| `app.py` | Presentation | Streamlit chat UI + optional voice input. No logic, no math, no AI calls. |
| `agent.py` | Routing | Anthropic tool-calling: understands intent, picks a tool, explains the JSON result. No math. |
| `test_tool.py` | Deterministic | Pure-Python data ingestion + all KPI math. Returns strict JSON. No AI. |
| `requirements.txt` | — | Pinned dependencies. |
| `.env.example` | — | Required credentials template (real keys never committed). |

**Run it:** copy `.env.example` → `.env` (add keys), `pip install -r requirements.txt`, then `streamlit run app.py`. Full instructions in [`../README.md`](../README.md).

---

## 2. Design / Architecture Document

The detailed write-up is in `docs/`:
- [`../docs/design.md`](../docs/design.md) — product overview, objective, scope, milestones.
- [`../docs/scoring-and-kpis.md`](../docs/scoring-and-kpis.md) — every KPI formula and the scoring model.
- [`../docs/data-and-apis.md`](../docs/data-and-apis.md) — the data sources and how they're accessed.
- [`../docs/architecture.md`](../docs/architecture.md) — the strict 3-tier code structure.

The three required points are answered below, in short.

### 2a. Scoring Methodology

Every number is computed deterministically in `test_tool.py` and returned as JSON — the LLM never produces a figure. Full formulas in [`../docs/scoring-and-kpis.md`](../docs/scoring-and-kpis.md).

**The KPIs** (from two data sources combined — flight movements + runway capacity):
- **TrafficVolume** — average daily movements over a 7-day window.
- **CapacityIndex** — a runway-based capacity proxy: `usable_runways × throughput/hr × operating_hours × length_factor`.
- **Utilization** = TrafficVolume / CapacityIndex — how full the airport is.
- **PeakSaturation** = busiest-hour load / hourly capacity — congestion is felt at peaks.
- **Growth** — traffic vs. a baseline ~3 months earlier (a recent trend, not year-over-year).
- **LongHaulShare** — share of departures ≥ 4,000 km (great-circle), with a duration fallback.
- **HourlyClipping** — fraction of operating hours hitting ≥ 90% of hourly capacity.
- **Confidence** — share of flights with complete data; drives how certain the language is.

**The ranking score (Q1).** For a set of airports, each KPI is **min-max normalized within the set** (0–1), then combined with a transparent weighted sum:

```
ExpansionScore = 100 × (0.40·Utilization + 0.30·Growth + 0.20·PeakSaturation + 0.10·LongHaulShare)
```

The weights are stated, tunable assumptions reflecting the investment thesis: **airports that are already full and still growing are the best expansion bets.** If a KPI is missing (e.g. no growth baseline), its term is dropped, the remaining weights are renormalized to sum to 1, and confidence is lowered — missing data is never treated as zero.

**Single-airport questions (Q2–Q4)** have no set to normalize against, so they report **absolute values plus category bands** (e.g. Utilization < 0.50 = Low, 0.50–0.85 = Moderate, …) instead of a 0–100 score.

### 2b. Key Tradeoffs

Honest choices made to fit a one-day build, each surfaced to the user rather than hidden:

| Tradeoff | Decision | Why |
|---|---|---|
| **No passenger data exists publicly** | Proxy demand with **flight movements** | Movements are the best available public signal; labeled as a proxy in every answer. |
| **No gate/terminal capacity data exists** | Proxy capacity from **runways** (count + length) | Runway infrastructure is the only free, reliable capacity signal; real terminal bottlenecks may differ, and we say so. |
| **OpenSky data is a crowdsourced sample** | Accept sampling; quantify it with a **Confidence score** | A census-grade source would cost money/time; the confidence KPI makes the uncertainty explicit. |
| **OpenSky credit + rate limits** | **Cache on disk**, fetch concurrently, **circuit breaker** on throttling, cap region size | Keeps the app fast and interactive without burning the daily credit budget. |
| **Growth depth** | **~90-day** baseline, not a full year | Fits the credit/time budget and confirmed data depth; the limitation (no seasonality adjustment) is noted. |
| **Polish vs. substance** | Invested in the scoring engine + AI routing, not UI styling | The assignment explicitly values reasoning over polish. |

### 2c. Where / How AI Is Used

The AI is a **router and explainer, never a calculator.** This is the central design decision.

- **Used for:** understanding the user's intent, resolving airport names/aliases, selecting the right tool via Anthropic **tool-calling**, and translating the deterministic JSON into a clear, analyst-style explanation with assumptions and confidence.
- **Never used for:** computing any number. A system-prompt "iron rule" forbids the model from quoting, estimating, or recalling a figure from memory — every quantitative value comes from a `test_tool.py` function.

**The flow:** user question → `agent.py` (Claude picks a tool + arguments) → `test_tool.py` (deterministic math → JSON) → `agent.py` (Claude explains the JSON) → answer.

This guarantees every figure is traceable to code, and means the AI model can be swapped or upgraded without touching the business logic. See [`../docs/architecture.md`](../docs/architecture.md) for the strict layer separation.

---

## Requirement checklist

- [x] **Source code** — `app.py`, `agent.py`, `test_tool.py` (+ run instructions in README).
- [x] **Scoring methodology** — §2a above and [`../docs/scoring-and-kpis.md`](../docs/scoring-and-kpis.md).
- [x] **Key tradeoffs** — §2b above and [`../docs/design.md`](../docs/design.md).
- [x] **Where/how AI is used** — §2c above and [`../docs/architecture.md`](../docs/architecture.md).
