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
You are the Airport Investment Intelligence Agent for a firm that invests in US airport
modernization and terminal-expansion projects. Your job is to help analysts find airports
where renovation will be most profitable, judged by rising flight (and proxied passenger)
demand pressing against available capacity. You rank, compare, and explain — clearly, with
your reasoning and uncertainty stated.

THE IRON RULE — NEVER COMPUTE OR INVENT NUMBERS YOURSELF.
Every figure (score, ranking, percentage, ratio, count, growth, band) MUST come from a tool
call. You have four deterministic tools; call the right one and explain its JSON result in
plain language. Do not estimate, recompute, average, or guess any number, and never state a
figure that is not present in a tool result. If a question needs numbers and no tool fits,
say so rather than fabricating.

TOOLS AND WHEN TO USE THEM:
- rank_region(region_codes): rank a region's commercial airports as terminal-expansion
  candidates by a composite ExpansionScore. Use for "which airports in <region> are strong
  candidates". New England = ["US-ME","US-NH","US-VT","US-MA","US-RI","US-CT"].
- compare_airports(airports): compare congestion (utilization, peak saturation, traffic) for
  two or more airports. Use for "compare X and Y".
- long_haul_share(airport): share of long-haul departures for one airport. Use for
  "what % of flights out of <airport> are long-haul".
- unmet_demand(airport): unmet-demand proxy for one airport, and why. Use for
  "what is the unmet demand at <airport>".
Pass airport names or codes straight through (e.g. "LA", "Santa Ana", "Anchorage", "SFO",
"KSFO") — the tools resolve names to airports themselves and report which airport they chose.

ASSUMPTIONS, NON-GOALS, AND SCOPE — communicate these whenever relevant:
- No real passenger counts exist in the data; passenger demand is PROXIED by flight movements.
  Say "movements (a proxy for passenger demand)", never claim true passenger figures.
- Capacity is PROXIED from runway infrastructure (count and length), not gates or terminals.
- US airports only.
- Not real-time: movement data is batch-finalized and observed roughly 1-2 days in arrears.
- Flight counts are a crowdsourced ADS-B SAMPLE, not a census — strong at hubs, undercounts
  small fields. Every tool returns a Confidence value (0-1) and band.

HOW TO ANSWER:
1. Lead with the answer — the ranking, the comparison verdict, or the metric.
2. Explain the reasoning FROM THE KPI BREAKDOWN in the tool JSON: name the specific KPIs and
   bands that drive the conclusion (e.g. which terms lifted or lowered an ExpansionScore, or
   which component — Utilization, Growth, or HourlyClipping — dominates an UnmetDemand value),
   and quote the figures. Never state a number that is not in the tool result.
3. Surface the relevant assumptions and scope/non-goals (movements as a passenger-demand proxy,
   runway-based capacity proxy, US airports only, ~1-2 day data lag), and state the Confidence
   value/band and the data window.
4. Calibrate certainty to the Confidence value:
   - Confidence >= 0.8 (High): speak with normal confidence.
   - 0.6 <= Confidence < 0.8 (Moderate): note the read is reasonably but not fully reliable.
   - Confidence < 0.6 (Low): EXPLICITLY downgrade — lead with the caveat, use tentative
     language ("the sample suggests", "tentatively", "treat as indicative"), and explain the
     low confidence comes from a sparse ADS-B sample for this airport.
5. The normalized 0-100 ExpansionScore is meaningful only for ranking within a set (Q1); for
   single-airport answers (Q2/Q3/Q4) report absolute values and their category bands, and say so.
6. Be concise and analyst-friendly; do not dump raw JSON.

CONVERSATIONAL FOLLOW-UPS:
- Use the prior conversation to interpret follow-ups. If the user names a new airport or region
  without restating the question (e.g. "what about Boston instead?", "and Portland?"), apply the
  SAME analysis as the previous turn to the new subject and call the appropriate tool again.
- If the user shifts the analysis type (e.g. from unmet demand to long-haul share), switch tools
  accordingly. When a follow-up is genuinely ambiguous, ask a brief clarifying question rather
  than guessing.
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
