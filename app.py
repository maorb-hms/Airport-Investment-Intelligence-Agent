"""Presentation Layer (Streamlit): chat UI + st.session_state only — no business logic, math, or Anthropic calls (architecture.md §1)."""

import streamlit as st

from agent import run_agent


st.set_page_config(
    page_title="Airport Investment Intelligence Agent",
    page_icon="✈️",
    layout="centered",
)

st.title("✈️ Airport Investment Intelligence Agent")
st.caption(
    "Find US airports where terminal expansion will pay off. Every figure is computed "
    "deterministically and explained with its assumptions, confidence, and scope."
)

with st.sidebar:
    st.header("Try asking")
    st.markdown(
        "- Compare LA and Santa Ana airport congestion levels.\n"
        "- What percentage of flights out of Anchorage are long-haul?\n"
        "- What is the unmet flight demand at SFO, and why?\n"
        "- Which airports in New England are strong candidates for terminal expansion?"
    )
    st.caption('Follow-ups work too — e.g. "What about Boston instead?"')
    if st.button("Clear conversation"):
        st.session_state.messages = []
        st.rerun()

# Chat history lives entirely in session state (architecture.md §1).
if "messages" not in st.session_state:
    st.session_state.messages = []

# Render the conversation so far.
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Capture input and delegate to the routing agent — no logic happens here.
user_input = st.chat_input("Ask about an airport or region…")
if user_input:
    with st.chat_message("user"):
        st.markdown(user_input)

    # History passed to the agent is every prior turn (not including this new message);
    # run_agent appends the new user message itself.
    prior_history = list(st.session_state.messages)
    st.session_state.messages.append({"role": "user", "content": user_input})

    with st.chat_message("assistant"):
        with st.spinner("Analyzing…"):
            answer = run_agent(user_input, prior_history)
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
