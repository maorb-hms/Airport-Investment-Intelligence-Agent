"""Presentation Layer (Streamlit): chat UI + st.session_state only — no business logic, math, or Anthropic calls (architecture.md §1)."""

import io

import streamlit as st

from agent import run_agent


def _transcribe(audio_bytes: bytes):
    """Best-effort voice→text. Returns the transcript, or None if no backend is
    installed or transcription fails. Voice is a non-blocking enhancement: this
    never raises, so the UI always falls back to text input (architecture.md §1)."""
    try:
        import speech_recognition as sr  # optional; not in requirements.txt
    except ImportError:
        return None
    try:
        recognizer = sr.Recognizer()
        with sr.AudioFile(io.BytesIO(audio_bytes)) as source:
            audio = recognizer.record(source)
        return recognizer.recognize_google(audio)  # free recognizer, no key
    except Exception:
        return None


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

    st.header("🎙️ Voice (optional)")
    st.caption("Record a question instead of typing. Falls back to text if unavailable.")
    voice_text = None
    audio_value = st.audio_input("Ask by voice", label_visibility="collapsed")
    if audio_value is not None:
        # Only transcribe a freshly recorded clip once (the widget returns the same
        # audio on every rerun until it's cleared).
        audio_bytes = audio_value.getvalue()
        audio_id = hash(audio_bytes)
        if st.session_state.get("last_audio_id") != audio_id:
            st.session_state.last_audio_id = audio_id
            with st.spinner("Transcribing…"):
                voice_text = _transcribe(audio_bytes)
            if voice_text is None:
                st.info(
                    "Voice transcription isn't available right now — type your "
                    "question below instead. (Install `SpeechRecognition` to enable it.)"
                )

    if st.button("Clear conversation"):
        st.session_state.messages = []
        st.session_state.pop("last_audio_id", None)
        st.rerun()

# Chat history lives entirely in session state (architecture.md §1).
if "messages" not in st.session_state:
    st.session_state.messages = []

# Render the conversation so far.
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Capture input and delegate to the routing agent — no logic happens here.
# A transcribed voice question (if any) flows through the same path as typed text.
user_input = st.chat_input("Ask about an airport or region…") or voice_text
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
