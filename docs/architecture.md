# System Architecture Guidelines - 3-Tier Agentic Model

You must strictly adhere to this 3-tier architecture for any file creation or modification. Do not mix responsibilities between files.

## 1. Presentation Layer (`app.py`)
- **Technology:** Streamlit
- **Responsibility:** Strictly handles the user interface (UI) components and chat interaction.
- **Rules:**
  - Must implement and maintain chat history using Streamlit's `st.session_state`.
  - Captures user text input and renders the final agent response.
  - **CRITICAL RESTRICTION:** Absolutely NO business logic, NO mathematical calculations, and NO direct Agent/Anthropic API calls are allowed in this file. It must only call the routing agent function.

## 2. Routing Layer (`agent.py`)
- **Technology:** Anthropic SDK / Anthropic Tool Calling API
- **Responsibility:** Acts as a traffic controller between user input and deterministic tools (The Agent's brain).
- **Rules:**
  - Contains the System Prompt and defines the JSON schemas for Anthropic's Tool Calling (`tools` parameter with `type: "function"`).
  - Analyzes user intent to dynamically route the request to the correct Python tool.
  - Takes the structured JSON output returned from the tool and translates/explains it into natural language for the user.
  - **CRITICAL RESTRICTION:** This layer must NOT perform any manual math or handle data source ingestion directly.

## 3. Deterministic Layer (`test_tool.py`)
- **Technology:** Pure Python (Pandas, Requests, etc.)
- **Responsibility:** Executes rigid, logical, and mathematical operations. The Single Source of Truth (SSOT).
- **Rules:**
  - Contains pure Python functions that interface with static data (e.g., CSV files) and dynamic data (e.g., APIs).
  - Performs all mathematical calculations and scores deterministically.
  - **Output Format:** Must always return a strict, structured JSON string back to the Routing Layer (`agent.py`).
  - **CRITICAL RESTRICTION:** This layer must remain completely independent of the AI Agent. It contains no AI components or Anthropic SDK dependencies.

## Version Control Rule
- Every time a layer is verified as functional in the terminal, a Git commit must be executed immediately before moving to the next task to prevent environment regressions.
