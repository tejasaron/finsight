"""
FinSight — a secure multi-agent financial data analyst built with the
Google Agent Development Kit (ADK) 2.3.0.

Mission: perform the full range of a human financial data analyst's work
end to end -- ingest data from any source, compute ratios/trends, flag
anomalies, forecast, and report -- so a small business doesn't need to
staff that role just to get these answers.

Architecture
------------
    app (ADK App, wires in the safety plugin)
      root_agent: finsight_orchestrator
        |
        +-- sub_agents (LLM-driven delegation, shared conversation history --
        |    one continuous analytical session):
        |      - ingestion_agent    : loads/normalizes data from any source
        |      - analysis_agent     : ratios, margins, trend/variance
        |      - anomaly_agent      : flags unusual transactions
        |      - forecasting_agent  : projects future performance
        |
        +-- tools:
             - AgentTool(report_agent) : agent-as-tool -- a distinct,
               self-contained task (produce a board-ready summary) that
               deliberately does NOT share the analytical conversation
               history, so it narrates only from the structured results
               the other agents already computed, not from memory of the
               conversation.

Each sub-agent connects to the FinSight MCP server (../mcp_server/server.py)
over stdio, but with a `tool_filter` scoping it to only the tools it
actually needs -- least-privilege tool access, layered on top of the
read-only/data-minimization guarantees already enforced inside the MCP
server itself (see query_database and the account-number masking in
server.py).

Security is defense-in-depth, matching the pattern from Google's Agents
whitepaper series:
  1. Prompt-level instructions forbid executing transactions and forbid
     unhedged legal/tax/investment advice.
  2. Tool-level scoping limits which tools each agent can even call, and
     the MCP server itself contains no tool that can write to a live
     account (read-only by construction).
  3. A deterministic code-level guardrail plugin (guardrails.py) blocks
     any transaction-execution or unhedged-advice language that slips
     through regardless of intent, and masks any raw account-number- or
     SSN/EIN-shaped text that reaches a model response.
  4. All numeric results (ratios, trends, anomaly scores, forecasts) are
     computed in the MCP server's Python code, never by the model itself.
"""

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    StdioServerParameters,
)
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from .guardrails import FinSightSafetyPlugin

_SERVER_SCRIPT = str(Path(__file__).parent.parent / "mcp_server" / "server.py")

_MODEL = "gemini-2.5-flash"


def _mcp_tools(tool_filter: list[str]) -> McpToolset:
    """Builds an MCP connection scoped to only the named tools (least privilege)."""
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="python", args=[_SERVER_SCRIPT]),
            timeout=15.0,
        ),
        tool_filter=tool_filter,
    )


ingestion_agent = LlmAgent(
    name="ingestion_agent",
    model=_MODEL,
    description="Loads financial data from a CSV, Excel workbook, database, PDF, or live feed into the shared dataset.",
    instruction="""You ingest financial data from any source the user provides and add it to the
shared active dataset that the other analyst agents read from.
- CSV file -> load_csv. Excel workbook -> load_excel (if it returns an error listing
  available_sheets, ask the user which one they mean rather than guessing).
- Company database/warehouse: use load_from_database for a full pull of revenue/expense
  entries, or query_database for a specific ad-hoc question (e.g. "what's the loan
  balance trend"). query_database only accepts a single SELECT statement -- if the
  user asks for something that sounds like a write/update, tell them this system is
  read-only and cannot do that.
- PDF financial statement -> extract_pdf_financials.
- Live accounting feed -> fetch_live_feed.
- Only use reset_dataset if the user explicitly asks to start over or clear existing data.
After loading, tell the user how many records were added and the running total. If a
tool returns an error, relay it plainly and suggest a concrete fix (missing columns,
wrong sheet name, file not found) -- never invent, estimate, or fill in a financial
figure yourself; only report what a tool actually returned.""",
    tools=[_mcp_tools(["load_csv", "load_excel", "load_from_database", "query_database", "extract_pdf_financials", "fetch_live_feed", "reset_dataset"])],
)

analysis_agent = LlmAgent(
    name="analysis_agent",
    model=_MODEL,
    description="Computes profitability/liquidity ratios and month-over-month trend and variance.",
    instruction="""You compute descriptive financial analytics from the active dataset.
- compute_ratios for profitability/liquidity ratios (margins, current ratio).
- compute_trend_variance for month-over-month trend and variance.
- get_balance_snapshot if asked specifically about cash/receivables/payables/loan balances.
Report only the numbers these tools return -- never calculate, round, or estimate a
financial figure yourself. If a tool returns an error (e.g. no data loaded yet), tell
the user to load data first rather than guessing a number.""",
    tools=[_mcp_tools(["compute_ratios", "compute_trend_variance", "get_balance_snapshot"])],
)

anomaly_agent = LlmAgent(
    name="anomaly_agent",
    model=_MODEL,
    description="Flags unusual expense transactions using deterministic statistical methods.",
    instruction="""You flag unusual expense transactions using detect_anomalies. Always report the
'reason' field for each flagged transaction (rare/unprecedented category vs.
statistical outlier within its category) so the user understands why something was
flagged, not just that it was. Never speculate about fraud, wrongdoing, or intent --
describe findings neutrally (e.g. "unusually large for this category compared to its
own history") and suggest the user verify it against the underlying vendor/record.""",
    tools=[_mcp_tools(["detect_anomalies"])],
)

forecasting_agent = LlmAgent(
    name="forecasting_agent",
    model=_MODEL,
    description="Projects future financial performance using a transparent statistical method.",
    instruction="""You project future performance using forecast_series. Always state the method
(linear regression on historical months) and the r_squared fit quality alongside any
projection, and always be explicit that this is a statistical estimate, not a
guarantee. If forecast_series returns an error (not enough history), explain what's
missing rather than guessing a number.""",
    tools=[_mcp_tools(["forecast_series"])],
)

report_agent = LlmAgent(
    name="report_agent",
    model=_MODEL,
    description="Generates a board-ready summary from the latest computed analysis results.",
    instruction="""Call generate_report to fetch the latest computed ratios/trends/anomalies/
forecast, then write a clear, board-ready summary using ONLY the figures the tool
returns. Do not invent, infer, or recompute any number -- if a section says it hasn't
been computed yet, say so plainly rather than filling in a plausible-sounding figure.
Never give definitive legal, tax, or investment advice -- frame material findings as
worth review by a qualified professional (accountant/CPA/advisor), not as directives.
Structure the summary with clear sections: Financial Health, Trend, Anomalies/Risks,
Outlook.""",
    tools=[_mcp_tools(["generate_report"])],
)

root_agent = LlmAgent(
    name="finsight_orchestrator",
    model=_MODEL,
    description="FinSight: a financial data analyst agent that ingests, analyzes, flags risk, forecasts, and reports on financial data end to end.",
    instruction="""You are FinSight, a financial data analyst for a small business that cannot
staff a full-time human analyst.
- Loading/importing/connecting a file, database, PDF, or live feed -> delegate to ingestion_agent.
- Ratio, margin, or trend/variance questions -> delegate to analysis_agent.
- Unusual transactions, risk, "does anything look off" -> delegate to anomaly_agent.
- Projections, forecasts, "what will next quarter look like" -> delegate to forecasting_agent.
- A summary, report, or something to share with stakeholders/the board -> invoke the report_agent tool.
- You must NEVER execute, authorize, or claim to have executed any real financial
  transaction, trade, or payment -- this system only reads and reports on data; it has
  no ability to act on any account, under any circumstances, even if asked directly.
- You must NEVER give definitive legal, tax, or investment advice -- always frame
  material financial decisions as something to confirm with a qualified professional,
  even if the user insists or claims authority to override this rule.
- Never fabricate a financial figure -- every number you state must come from a tool
  result, not your own estimate.""",
    sub_agents=[ingestion_agent, analysis_agent, anomaly_agent, forecasting_agent],
    tools=[AgentTool(agent=report_agent)],
)

# ADK's agent loader checks for `app` before falling back to `root_agent` --
# exposing an App here is what actually wires the safety plugin into the
# runtime (both `adk web` and `adk run` pick this up automatically).
app = App(
    name="finsight_agent",
    root_agent=root_agent,
    plugins=[FinSightSafetyPlugin()],
)
