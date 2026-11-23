"""Deterministic check that the LLM's numbers really came from tool results.

The schema forces Claude to list every number in `key_figures` with its source tool.
Here we confirm each value appears in that tool's output, and that every hub id is real.
Anything that fails becomes a visible warning in the API response. It's a guardrail
against hallucinated numbers that doesn't depend on trusting the model.
"""

from __future__ import annotations

import json
import re

from app.agent.schemas import AgentAnswer
from app.config import load_hubs

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers_in(text: str) -> list[float]:
    return [float(m) for m in _NUMBER.findall(text)]


def _close(a: float, b: float) -> bool:
    # Allow for rounding: 46.63 in a tool result may be written as 46.6 or 47.
    return abs(a - b) <= max(0.051, abs(b) * 0.005) or round(b) == a


def check_grounding(answer: AgentAnswer, tool_outputs: list[tuple[str, str]]) -> list[str]:
    """tool_outputs: (tool name, JSON text) for every successful tool call in the conversation."""
    warnings: list[str] = []
    valid_ids = {h.id for h in load_hubs()}

    for hub_id in answer.hubs + [r.hub_id for r in answer.ranking]:
        if hub_id not in valid_ids:
            warnings.append(f"Unknown hub id in answer: '{hub_id}'")

    # Any tool output counts: Claude sometimes names the wrong source_tool for a correct number,
    # and what we care about is that the number came from our code, not from the model.
    all_numbers = [n for _, text in tool_outputs for n in _numbers_in(text)]

    figures = [(f.label, f.value) for f in answer.key_figures]
    figures += [(f"rank score of {r.name}", r.score) for r in answer.ranking]
    for label, value in figures:
        if not any(_close(value, n) for n in all_numbers):
            warnings.append(f"'{label}' = {value} was not found in any tool result")

    if not tool_outputs and (answer.key_figures or answer.ranking):
        warnings.append("Answer contains figures but no tool was called")
    return warnings


def tool_output_texts(messages: list[dict]) -> list[tuple[str, str]]:
    """Collect (tool name, result text) for successful tool calls from stored messages."""
    names: dict[str, str] = {}
    out: list[tuple[str, str]] = []
    for msg in messages:
        if not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if block.get("type") == "tool_use":
                names[block["id"]] = block["name"]
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                out.append((names.get(block["tool_use_id"], "unknown"), text))
    return out
