"""
Closr — V2 Deep Research Harness (ReAct State Machine)

Drives Qwen 2.5 7B through a stateful, tool-calling enrichment loop.
One harness instance per company entity.

Architecture (from ReAct.md):
  - Python holds the research_state dict externally — never trust LLM memory
  - Prompt is REBUILT on every iteration from current state
  - Ollama called with format="json" (Option A grammar enforcement)
  - Insanity Check: hash(action + action_input) vs failed_attempts
  - Context Compressor: summarize large tool outputs before appending to state
  - Hard Kill: force conclude_research(status=failed) at max_iterations

Loop flow per iteration:
  1. Build prompt from state
  2. Call Ollama (format=json)
  3. Parse LLM output → {synthesis, plan, action, action_input}
  4. Insanity Check
  5. Execute tool via pipeline.tools.execute_tool()
  6. Compress observation if too large
  7. Update state (discovered_facts, failed_attempts, iteration_count)
  8. Break if action == conclude_research or count >= max
"""

import hashlib
import json
import logging
import re
import threading
from typing import Any

import requests

from config import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    REACT_MAX_ITERATIONS,
    REACT_SCORE_FIT_THRESHOLD,
)
from pipeline.tools import execute_tool, score_fit

logger = logging.getLogger("closr.pipeline.harness")

# Max chars allowed in a single observation before compression is applied
_MAX_OBSERVATION_CHARS = 500

# Ollama endpoint
_OLLAMA_CHAT_URL = f"{OLLAMA_BASE_URL}/api/chat"
_ollama_lock = threading.Lock()

# System prompt template — injected with state on every iteration
_SYSTEM_PROMPT = """You are an elite, autonomous B2B OSINT agent for Closr.
Your objective: research the target company and find the best contact for creator outreach.
You operate STRICTLY in JSON. No prose. No markdown. Pure JSON only.

AVAILABLE TOOLS:
1. "osint_search"           : {{"query": "string"}}            → Google search, returns snippets
2. "fetch_linkedin_title"   : {{"linkedin_url": "string"}}     → Get real title from LinkedIn profile
3. "validate_email_endpoint": {{"email": "string"}}            → Returns true/false
4. "discover_email"         : {{"first_name": "str", "last_name": "str", "domain": "str", "linkedin_url": "str"}} → Searches Prospeo/Snov/Hunter
5. "normalize_title"        : {{"raw_text": "string"}}         → Returns cleaned title string
6. "assign_proximity_rank"  : {{"title": "string"}}            → Returns rank int (1=best)
7. "score_fit"              : {{"company_info": {{...}}}}       → Returns ICP score 1-5
8. "conclude_research"      : {{"status": "success|failed", "lead_data": {{"first_name": "str", "last_name": "str", "title": "str", "email": "str"}}}} → ENDS LOOP

REQUIRED OUTPUT SCHEMA (respond with ONLY this JSON, no other text):
{{
  "synthesis": "What do the discovered facts tell you so far?",
  "plan": "What is your immediate next action and why?",
  "action": "tool_name",
  "action_input": {{"key": "value"}}
}}"""

_STATE_INJECTION = """
CURRENT RESEARCH STATE:
Goal: {goal}
Company: {company} (domain: {domain})
Iteration: {iteration}/{max_iterations}

Discovered Facts:
{facts}

Failed Attempts (DO NOT REPEAT THESE EXACT CALLS):
{failed}
"""


def _hash_action(action: str, action_input: dict) -> str:
    """Deterministic hash of an (action, action_input) pair for insanity check."""
    payload = json.dumps({"action": action, "action_input": action_input}, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()


def _compress_observation(text: Any) -> str:
    """
    Compress a large tool observation to avoid blowing out Qwen's context window.
    For list results (osint_search), summarize to top 3 snippets.
    For long strings, truncate with a marker.
    """
    if isinstance(text, list):
        # osint_search returns a list of {title, link, snippet}
        compressed = []
        for item in text[:3]:
            if isinstance(item, dict):
                snippet = item.get("snippet", "")[:200]
                link = item.get("link", "")
                title = item.get("title", "")
                compressed.append(f"• [{title}] {link} — {snippet}")
            else:
                compressed.append(str(item)[:200])
        return "\n".join(compressed)

    text_str = str(text)
    if len(text_str) > _MAX_OBSERVATION_CHARS:
        return text_str[:_MAX_OBSERVATION_CHARS] + "… [truncated]"
    return text_str


def _call_ollama(messages: list[dict]) -> str | None:
    """
    Call the Ollama chat endpoint with format=json.
    Returns the raw content string or None on failure.
    """
    try:
        with _ollama_lock:
            response = requests.post(
                _OLLAMA_CHAT_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "format": "json",       # Option A: Ollama JSON mode
                    "stream": False,
                    "options": {
                        "temperature": 0.0,  # Deterministic output for tool calls
                        "num_predict": 1024,
                        "num_ctx": 4096,
                    },
                },
                timeout=60,
            )
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "")
    except Exception as e:
        logger.error(f"Harness: Ollama call failed — {e}")
        return None


def _parse_llm_output(raw: str) -> dict | None:
    """
    Parse the LLM's JSON output. Uses the existing JSON repair approach.
    Returns dict with keys: synthesis, plan, action, action_input.
    Returns None if unparseable after all attempts.
    """
    if not raw:
        return None

    # Attempt 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Attempt 2: extract first JSON object from the string
    try:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except (json.JSONDecodeError, AttributeError):
        pass

    logger.warning(f"Harness: Could not parse LLM output: {raw[:200]}")
    return None


class DeepResearchHarness:
    """
    Stateful ReAct loop for a single company enrichment target.

    Usage:
        harness = DeepResearchHarness(entity_dict)
        result = harness.run()
        # result: {"status": "success"|"failed", "lead_data": {...}}
    """

    def __init__(self, entity: dict):
        """
        Args:
            entity: The extracted entity dict from llm_worker, containing:
                    company_name, company_data (with domain, niche, signal_type,
                    signal_summary), entity (with contacts).
        """
        self.entity = entity
        company_data = entity.get("company_data") or {}

        self.state: dict = {
            "target_company": entity.get("company_name", "Unknown"),
            "target_domain": company_data.get("domain", "unknown"),
            "goal": (
                f"Find the best creator economy decision-maker at "
                f"{entity.get('company_name', 'this company')} "
                f"and locate a verified contact method."
            ),
            "discovered_facts": [],
            "failed_attempts": [],     # list of hashed (action, action_input)
            "iteration_count": 0,
            "max_iterations": REACT_MAX_ITERATIONS,
            "result": None,
        }

    # ─────────────────────────────────────────────────────
    # Score gate: skip enrichment if ICP score < threshold
    # ─────────────────────────────────────────────────────
    def _passes_score_gate(self) -> bool:
        company_data = self.entity.get("company_data") or {}
        # signal lives at entity.signal, NOT company_data — company_data is a stripped copy
        entity_obj = self.entity.get("entity") or {}
        signal = entity_obj.get("signal") or {}
        info = {
            "name": self.entity.get("company_name", ""),
            "niche": company_data.get("niche", ""),
            "signal_summary": signal.get("summary", ""),
            "signal_type": signal.get("type", ""),
        }
        score = score_fit(info)
        logger.info(
            f"Harness: ICP score for '{info['name']}' = {score}/5 "
            f"(niche='{info['niche']}', signal_type='{info['signal_type']}')"
        )
        if score < REACT_SCORE_FIT_THRESHOLD:
            logger.info(
                f"Harness: score {score} < threshold {REACT_SCORE_FIT_THRESHOLD} "
                f"— skipping enrichment for '{info['name']}'."
            )
            return False
        return True

    # ─────────────────────────────────────────────────────
    # Prompt builder
    # ─────────────────────────────────────────────────────
    def _build_prompt(self) -> list[dict]:
        s = self.state
        facts_str = (
            "\n".join(f"  - {f}" for f in s["discovered_facts"])
            if s["discovered_facts"]
            else "  (none yet)"
        )
        failed_str = (
            "\n".join(f"  - {f}" for f in s["failed_attempts"][-5:])
            if s["failed_attempts"]
            else "  (none)"
        )
        state_block = _STATE_INJECTION.format(
            goal=s["goal"],
            company=s["target_company"],
            domain=s["target_domain"],
            iteration=s["iteration_count"] + 1,
            max_iterations=s["max_iterations"],
            facts=facts_str,
            failed=failed_str,
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": state_block.strip()},
        ]

    # ─────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────
    def run(self) -> dict:
        """
        Execute the ReAct loop. Returns the final lead result dict.
        {"status": "success"|"failed"|"skipped", "lead_data": {...}}
        """
        # ── Score Gate ───────────────────────────────────
        if not self._passes_score_gate():
            return {"status": "skipped", "lead_data": {}}

        logger.info(
            f"Harness: Starting ReAct loop for '{self.state['target_company']}' "
            f"(max {REACT_MAX_ITERATIONS} iterations)"
        )

        while self.state["iteration_count"] < REACT_MAX_ITERATIONS:
            self.state["iteration_count"] += 1
            iteration = self.state["iteration_count"]
            logger.info(
                f"Harness [{self.state['target_company']}]: "
                f"Iteration {iteration}/{REACT_MAX_ITERATIONS}"
            )

            # ── Build & call LLM ─────────────────────────
            messages = self._build_prompt()
            raw_output = _call_ollama(messages)

            if raw_output is None:
                logger.warning("Harness: Ollama unreachable — aborting loop.")
                break

            parsed = _parse_llm_output(raw_output)
            if not parsed:
                logger.warning(f"Harness: Unparseable LLM output on iteration {iteration}.")
                self.state["discovered_facts"].append(
                    f"[ERROR] Iteration {iteration}: LLM output was not valid JSON. Retry."
                )
                continue

            action = parsed.get("action", "")
            action_input = parsed.get("action_input", {})
            synthesis = parsed.get("synthesis", "")
            plan = parsed.get("plan", "")

            logger.debug(
                f"Harness: synthesis='{synthesis[:80]}' | "
                f"plan='{plan[:80]}' | action='{action}'"
            )

            # ── Insanity Check ───────────────────────────
            action_hash = _hash_action(action, action_input)
            if action_hash in self.state["failed_attempts"]:
                logger.info(
                    f"Harness: Insanity Check triggered — "
                    f"'{action}' with same args already attempted."
                )
                self.state["discovered_facts"].append(
                    f"[BLOCKED] You already tried action='{action}' with these exact args. "
                    f"You MUST try a different approach."
                )
                continue

            # ── Terminal Action ──────────────────────────
            if action == "conclude_research":
                result = execute_tool("conclude_research", action_input)
                final = result.get("result") or result
                self.state["result"] = final
                logger.info(
                    f"Harness: conclude_research called — "
                    f"status={final.get('status')} for '{self.state['target_company']}'"
                )
                return final

            # ── Execute Tool ─────────────────────────────
            tool_result = execute_tool(action, action_input)

            if "error" in tool_result:
                observation = f"[TOOL ERROR] {tool_result['error']}"
                self.state["failed_attempts"].append(action_hash)
                logger.debug(f"Harness: tool error — {tool_result['error']}")
            else:
                raw_obs = tool_result.get("result", "")
                observation = _compress_observation(raw_obs)
                logger.debug(f"Harness: tool success — obs={observation[:100]}")

            # ── Update State ─────────────────────────────
            self.state["discovered_facts"].append(
                f"[Iter {iteration}] {action}({action_input}) → {observation}"
            )

        # ── Hard Kill ────────────────────────────────────
        logger.warning(
            f"Harness: Max iterations reached for '{self.state['target_company']}'. "
            f"Forcing conclude_research(status=failed)."
        )
        return {
            "status": "failed",
            "lead_data": {
                "company_name": self.state["target_company"],
                "domain": self.state["target_domain"],
                "discovered_facts": self.state["discovered_facts"],
            },
        }
