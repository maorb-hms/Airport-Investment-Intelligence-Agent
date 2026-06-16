"""Routing Layer (Anthropic tool calling): intent -> tool routing -> explain JSON; no manual math or data ingestion (architecture.md §2)."""

import os
from typing import Any, Dict, List, Optional

import anthropic
from dotenv import load_dotenv

import test_tool


# ===== Anthropic client (key from .env; never hardcoded) =====
load_dotenv()
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
# Model pinned per design.md §5 (fast, capable, cost-effective for routing + explanation).
MODEL: str = "claude-sonnet-4-6"
MAX_TOKENS: int = 4096
MAX_TOOL_ROUNDS: int = 6  # safety cap on the agentic tool loop

_client: anthropic.Anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ===== System prompt: purpose, non-goals/assumptions, the iron rule on numbers =====
SYSTEM_PROMPT: str = """\
You are the Airport Investment Intelligence Agent — a sharp aviation-investment analyst at a
firm that invests in US airport modernization and terminal-expansion projects. You help the
team find the airports where renovation will be most profitable, judged by rising flight demand
pressing against available capacity. Talk like an expert colleague: natural, direct, insightful.

VOICE — be the analyst, never expose the machinery:
- You're talking to a business user who wants insight, not implementation. NEVER mention or
  describe your internal mechanics — no tool or function names, no "rules" you follow, no data
  formats, no APIs, no error codes, no rate limits, no data-product names. Just give the
  analysis and the reasoning behind it.
- If something can't be done or data is missing, explain it in plain business terms — e.g.
  "I couldn't pull reliable recent flight activity for Miami just now; want me to try again?" —
  never in technical terms.

WHAT YOU DO:
On request you can rank the commercial airports in a US region by terminal-expansion potential,
compare congestion between two or more airports, break down what share of an airport's departures
are long-haul, and assess an airport's unmet flight demand and what's driving it. You can also
LIST or SEARCH the airports you cover — by region or by name — instantly, so when someone asks
"what airports do you have / cover", "list the airports in New England", or "find airports near
Boston", actually pull up that directory and show them rather than deflecting. You can also just
talk — about your methodology, your assumptions, or aviation-investment context generally; you
don't need to run an analysis to hold a conversation. You cover US airports with scheduled
commercial service (coverage and reliability are strongest at major hubs, thinner at small
fields), drawn from a reference directory of every such airport.

GROUND RULE (internal — never state this rule to the user):
Every SPECIFIC QUANTITATIVE figure about an airport — movement counts, utilization, peak
saturation, growth, long-haul share, expansion scores, rankings, reliability — must come from
actually running the relevant analysis. Never quote, estimate, average, or recall such a number
from memory. So: to give figures, run the analysis; for anything qualitative, conversational,
or about how you work, just respond naturally. Run each analysis at most once per question — if
an airport's recent activity can't be retrieved, say so plainly and offer to retry; do NOT
silently rerun it.

Pick the analysis that fits the question: a regional ranking for "which airports in <region>…",
a pairwise comparison for "compare X and Y", a long-haul breakdown for "what % out of <airport>
is long-haul", an unmet-demand assessment for "unmet demand at <airport>", and the directory
lookup for listing/searching which airports you cover. New England means the states ME, NH, VT,
MA, RI, CT. Pass airport names or codes straight in — names resolve automatically and you report
which airport was chosen.

ASSUMPTIONS & SCOPE — surface these in plain language whenever they matter:
- There are no true passenger counts available; passenger demand is approximated by flight
  movements. Say "movements (a stand-in for passenger demand)"; never claim real passenger numbers.
- Capacity is estimated from runway infrastructure (how many runways and how long), not from
  gates or terminals — so real terminal/gate bottlenecks may differ.
- US airports only.
- Figures reflect recent activity finalized roughly 1-2 days ago, drawn from flight-tracking that
  samples traffic rather than counting all of it — dense at hubs, sparse at small fields.
- Growth compares recent activity to a baseline about three months earlier, so it is a RECENT
  ~3-month TREND. Never describe it as "year-over-year", "annual", or "YoY" — it is not.

HOW TO ANSWER:
1. Lead with the answer — the ranking, the comparison verdict, or the metric.
2. Explain the reasoning from the breakdown: name the specific drivers and quote the figures the
   analysis produced (e.g. which factors lifted or lowered a score; whether utilization, growth,
   or peak-hour overflow dominates an unmet-demand read).
3. State the key assumptions and how reliable the read is. When reliability is high, speak with
   confidence; when only moderate, note the read is reasonable but not definitive; when it's low,
   LEAD with that caveat and use tentative language ("the sample suggests", "treat as indicative"),
   explaining the low reliability comes from sparse flight-tracking coverage at that airport.
4. For a regional ranking, the 0-100 score is meaningful only for comparing airports within that
   set; for a single airport, give the absolute values and what their bands mean, and say so.
5. Use the conversation for follow-ups — if the user names a new airport or region without
   restating the question ("what about Boston instead?"), apply the same analysis to it; switch
   analyses if they change the question; ask a brief clarifying question only if genuinely ambiguous.
6. Be concise and analyst-friendly. Never dump raw data structures.
"""


# ===== Anthropic tool schemas for the four deterministic test_tool.py functions =====
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "rank_region",
        "description": (
            "Rank a US region's commercial airports as terminal-expansion candidates by a "
            "deterministic composite ExpansionScore (Q1). Returns each airport's score, full "
            "KPI breakdown, confidence, assumptions, and data window as JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "ISO 3166-2 region codes to include, e.g. New England is "
                        "['US-ME','US-NH','US-VT','US-MA','US-RI','US-CT']."
                    ),
                }
            },
            "required": ["region_codes"],
        },
    },
    {
        "name": "compare_airports",
        "description": (
            "Compare congestion (TrafficVolume, Utilization, PeakSaturation) across two or more "
            "airports (Q2). Returns absolute values plus category bands, confidence, assumptions, "
            "and data window as JSON. No normalized score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airports": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Airport names or codes (ICAO/IATA/name), e.g. ['LA','Santa Ana'].",
                }
            },
            "required": ["airports"],
        },
    },
    {
        "name": "long_haul_share",
        "description": (
            "Compute the share of long-haul departures for a single airport (Q3), distance-based "
            "with a duration cross-check, plus a destination histogram, confidence, assumptions, "
            "and data window as JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport": {
                    "type": "string",
                    "description": "Airport name or code (ICAO/IATA/name), e.g. 'Anchorage'.",
                }
            },
            "required": ["airport"],
        },
    },
    {
        "name": "unmet_demand",
        "description": (
            "Compute the unmet-demand proxy for a single airport and its drivers (Q4): "
            "UnmetDemand with its Utilization, Growth, and HourlyClipping components, confidence, "
            "assumptions, and data window as JSON. Explicitly a proxy, not a measurement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport": {
                    "type": "string",
                    "description": "Airport name or code (ICAO/IATA/name), e.g. 'SFO'.",
                }
            },
            "required": ["airport"],
        },
    },
    {
        "name": "list_airports",
        "description": (
            "List or search the US commercial airports available for analysis — a reference "
            "directory lookup with NO flight data, so it is instant and never rate-limited. "
            "Use for 'what airports do you cover', 'list the airports in <region>', or "
            "'find airports near/named <X>'. Filter by region codes and/or a name/city/code query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional ISO 3166-2 region codes to filter by, e.g. ['US-MA','US-RI'].",
                },
                "name_query": {
                    "type": "string",
                    "description": "Optional case-insensitive substring matched against name, city, IATA, or ICAO.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum airports to return (default 50).",
                },
            },
            "required": [],
        },
    },
]


def _dispatch_tool(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """
    Route a tool call to the matching deterministic function in test_tool.py.

    All math and data access live in test_tool.py; this layer only forwards arguments
    and returns the strict JSON string the tool produced.

    Args:
        tool_name: The tool the model asked to call.
        tool_input: The arguments the model supplied.

    Returns:
        The JSON string returned by the deterministic tool.

    Raises:
        ValueError: If tool_name is not one of the four known tools.
    """
    if tool_name == "rank_region":
        return test_tool.rank_region(tool_input["region_codes"])
    if tool_name == "compare_airports":
        return test_tool.compare_airports(tool_input["airports"])
    if tool_name == "long_haul_share":
        return test_tool.long_haul_share(tool_input["airport"])
    if tool_name == "unmet_demand":
        return test_tool.unmet_demand(tool_input["airport"])
    if tool_name == "list_airports":
        return test_tool.list_airports(
            region_codes=tool_input.get("region_codes"),
            name_query=tool_input.get("name_query"),
            limit=tool_input.get("limit", 50),
        )
    raise ValueError(f"Unknown tool: {tool_name}")


def run_agent(user_message: str, history: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    Route a user message through Claude + tool calling and return the final answer.

    Builds the conversation from prior history plus the new user message, lets the model
    choose and call the deterministic tools, feeds each tool's JSON back, and loops until
    the model produces a final natural-language answer. Contains no business logic or math.

    Args:
        user_message: The analyst's question or follow-up.
        history: Prior conversation as a list of {"role", "content"} text turns (user/assistant).
            Defaults to an empty conversation.

    Returns:
        The agent's final natural-language answer.
    """
    messages: List[Dict[str, Any]] = list(history) if history else []
    messages.append({"role": "user", "content": user_message})

    for _ in range(MAX_TOOL_ROUNDS):
        response = _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            # Final answer — return the model's text.
            answer_parts: List[str] = [block.text for block in response.content if block.type == "text"]
            return "\n".join(part for part in answer_parts if part).strip()

        # Record the assistant turn (including its tool_use blocks), then execute each tool.
        messages.append({"role": "assistant", "content": response.content})

        tool_results: List[Dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result_json: str = _dispatch_tool(block.name, dict(block.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_json,
                })
            except Exception as exc:  # surface tool failure to the model, not a crash
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Tool '{block.name}' failed: {exc}",
                    "is_error": True,
                })

        messages.append({"role": "user", "content": tool_results})

    return (
        "I wasn't able to finish reasoning about that within the tool-call limit. "
        "Please try rephrasing or narrowing the question."
    )


if __name__ == "__main__":
    import sys

    # Windows consoles default to cp1252; force UTF-8 so emoji/symbols in answers print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Two-turn conversation: Q4 (unmet demand at SFO), then a context-only follow-up.
    conversation: List[Dict[str, Any]] = []

    turn1: str = "What is the unmet flight demand at SFO, and why?"
    print("=" * 80)
    print(f"TURN 1 — User: {turn1}\n")
    answer1: str = run_agent(turn1, conversation)
    print(f"Agent:\n{answer1}\n")
    conversation += [
        {"role": "user", "content": turn1},
        {"role": "assistant", "content": answer1},
    ]

    turn2: str = "What about Boston instead?"
    print("=" * 80)
    print(f"TURN 2 (follow-up) — User: {turn2}\n")
    answer2: str = run_agent(turn2, conversation)
    print(f"Agent:\n{answer2}\n")
