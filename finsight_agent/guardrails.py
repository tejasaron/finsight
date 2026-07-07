"""
FinSightSafetyPlugin: deterministic, code-level guardrails that sit
*outside* the LLM's own reasoning.

This implements the "defense-in-depth" pattern described in Google's Agents
whitepaper series: never rely solely on prompt instructions to enforce a
hard safety rule. Three rules are enforced here regardless of what the
model itself decided:
  1. The agent must never claim to have executed, or offer to execute, a
     real financial transaction/trade/payment -- this system is read-only
     by construction (see mcp_server/server.py), and this is the backstop
     in case a prompt-injected or misbehaving model claims otherwise.
  2. The agent must never give unhedged, definitive legal/tax/investment
     advice -- material decisions should always be framed as something to
     confirm with a qualified professional.
  3. Any raw account-number/SSN/EIN-shaped text that reaches a model
     response is masked before the user sees it, as a last-resort layer on
     top of the masking/data-minimization already done in the MCP server's
     tool layer.

Verified against google-adk==2.3.0's `BasePlugin` interface:
  - before_model_callback(self, *, callback_context, llm_request) -> Optional[LlmResponse]
  - after_model_callback(self, *, callback_context, llm_response) -> Optional[LlmResponse]
Returning a non-None LlmResponse short-circuits the model call / overrides
the model's output for that turn.
"""

import logging
import re
from typing import Optional

from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

logger = logging.getLogger("finsight.guardrails")

# Heuristic patterns for a small, fast, deterministic classifier layer.
# In a production system this would likely be backed by a model-based
# classifier in addition to these regexes -- see the Agent Tools & MCP
# whitepaper's discussion of hybrid guardrails.
_PROMPT_INJECTION_PATTERNS = [
    r"\bignore\b.{0,30}\b(instructions|prompt|rules)\b",
    r"\byou are now\b",
    r"\bdisregard\b.{0,30}\b(rules|guidelines|system prompt)\b",
    r"\breveal\b.{0,20}\b(system prompt|your instructions)\b",
]

_TRANSACTION_EXECUTION_PATTERNS = [
    r"\bi(?:'ve| have)?\s*(?:just\s*)?(?:executed|submitted|placed|processed|initiated)\b.{0,25}\b(trade|order|payment|transfer|wire|withdrawal)\b",
    r"\bi(?:'ll| will)\s*(?:go ahead and\s*)?(?:transfer|wire|pay|withdraw|move)\b.{0,25}\b(funds|money|account)\b",
    r"\byour\s*(?:payment|transfer|wire|trade|order)\s*(?:has been|is being)\s*(?:sent|processed|submitted|executed)\b",
]

_DEFINITIVE_ADVICE_PATTERNS = [
    r"\byou (?:will|must) owe exactly\b",
    r"\bguaranteed\b.{0,15}\b(return|profit|gain)\b",
    r"\byou (?:must|have to)\b.{0,20}\bfor tax purposes\b",
    r"\bthis is (?:definitely|certainly)\b.{0,15}\b(legal|tax[- ]deductible|allowed)\b",
]

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")
_ACCOUNT_NUMBER_RE = re.compile(r"\b\d[\d\- ]{10,20}\d\b")

TRANSACTION_BLOCK_MESSAGE = (
    "I can't execute, authorize, or confirm any real financial transaction, trade, "
    "or payment -- I only read and report on data. Moving money or placing a trade "
    "has to happen through your bank, broker, or accounting system directly."
)

ADVICE_BLOCK_MESSAGE = (
    "I can't give definitive legal, tax, or investment advice. For a decision like "
    "this, please confirm with a qualified accountant, tax professional, or "
    "financial advisor -- I can share what the data shows, but the final call "
    "should come from a professional who knows your full situation."
)

INJECTION_BLOCK_MESSAGE = "I can't follow instructions that try to override my safety rules."


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _mask_sensitive_ids(text: str) -> tuple[str, bool]:
    """Masks any SSN/EIN/account-number-shaped run of digits to its last 4 digits."""
    changed = False

    def _mask_match(m: re.Match) -> str:
        nonlocal changed
        changed = True
        digits = re.sub(r"\D", "", m.group(0))
        return "*" * max(0, len(digits) - 4) + digits[-4:]

    text = _SSN_RE.sub(_mask_match, text)
    text = _EIN_RE.sub(_mask_match, text)
    text = _ACCOUNT_NUMBER_RE.sub(_mask_match, text)
    return text, changed


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


class FinSightSafetyPlugin(BasePlugin):
    """Reusable ADK plugin bundling FinSight's input/output guardrails."""

    def __init__(self):
        super().__init__(name="finsight_safety_plugin")

    async def before_model_callback(self, *, callback_context, llm_request) -> Optional[LlmResponse]:
        """Screens the incoming turn for prompt-injection attempts before the LLM ever sees it."""
        user_text = _extract_last_user_text(llm_request)
        if user_text and _matches_any(user_text, _PROMPT_INJECTION_PATTERNS):
            logger.warning("Blocked suspected prompt injection: %r", user_text[:200])
            return _text_response(INJECTION_BLOCK_MESSAGE)
        return None  # allow the call to proceed unchanged

    async def after_model_callback(self, *, callback_context, llm_response) -> Optional[LlmResponse]:
        """Screens the outgoing model response for hard-rule violations and redacts sensitive IDs."""
        response_text = _extract_response_text(llm_response)
        if not response_text:
            return None

        if _matches_any(response_text, _TRANSACTION_EXECUTION_PATTERNS):
            logger.warning("Blocked transaction-execution language in model output: %r", response_text[:200])
            return _text_response(TRANSACTION_BLOCK_MESSAGE)

        if _matches_any(response_text, _DEFINITIVE_ADVICE_PATTERNS):
            logger.warning("Blocked unhedged definitive-advice language in model output: %r", response_text[:200])
            return _text_response(ADVICE_BLOCK_MESSAGE)

        masked_text, changed = _mask_sensitive_ids(response_text)
        if changed:
            logger.warning("Masked a sensitive-ID-shaped value in model output before returning it.")
            return _text_response(masked_text)

        return None


def _extract_last_user_text(llm_request) -> str:
    """Best-effort extraction of the latest user message text from an LlmRequest.contents list."""
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        role = getattr(content, "role", None)
        if role == "user":
            parts = getattr(content, "parts", None) or []
            return " ".join(getattr(p, "text", "") or "" for p in parts)
    return ""


def _extract_response_text(llm_response) -> str:
    content = getattr(llm_response, "content", None)
    if not content:
        return ""
    parts = getattr(content, "parts", None) or []
    return " ".join(getattr(p, "text", "") or "" for p in parts)
