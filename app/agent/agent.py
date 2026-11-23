"""The agent loop: Claude + our deterministic tools + an enforced JSON answer.

Flow for one user message:
  1. Load the session's earlier messages from SQLite (follow-up context).
  2. Call Claude with the tools and the AgentAnswer JSON schema (output_config.format).
  3. While Claude asks for tools: run them locally and send the results back.
  4. Claude's final text must be AgentAnswer JSON. Validate it with Pydantic, check its
     numbers against the tool results (grounding.py), save the session, and return.

Why a hand-written loop instead of the SDK's beta tool runner: we want to record each tool
call (for the API response and the evals), cap the number of rounds, and validate the final
JSON ourselves. That's about 40 lines of loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.agent import tools
from app.agent.grounding import check_grounding, tool_output_texts
from app.agent.prompts import build_system_prompt
from app.agent.schemas import AgentAnswer
from app.config import get_settings
from app.storage import db

MODEL = "claude-opus-5-5"
EFFORT = "medium"  # enough reasoning for multi-tool questions, keeps chat latency reasonable
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 8  # safety cap; the example questions need 1-3 rounds
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# One lock per chat session, shared by every agent instance in the process. The API creates
# a short-lived agent per request (each with its own SQLite connection), so the locks live
# here: two requests for the same session wait for each other instead of mixing history.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class AgentError(RuntimeError):
    """The agent couldn't produce an answer (API down, refusal, invalid output, ...)."""


class ToolCall(BaseModel):
    name: str
    input: dict
    ok: bool
    duration_ms: int


class AgentResponse(BaseModel):
    session_id: str
    answer: AgentAnswer
    tool_calls: list[ToolCall] = Field(description="Tools called while answering this message")
    grounding_warnings: list[str] = Field(description="Numbers/hubs the check couldn't verify")
    model: str
    usage: dict[str, int]
    latency_ms: int


def _answer_schema() -> dict:
    return {"type": "json_schema", "schema": AgentAnswer.model_json_schema()}


class WeatherRiskAgent:
    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        client: anthropic.Anthropic | None = None,
        model: str = MODEL,
    ) -> None:
        self.conn = conn or db.connect()
        key = get_settings().anthropic_api_key or None  # None -> SDK reads the environment
        self.client = client or anthropic.Anthropic(api_key=key)
        self.model = model
        self._tool_defs = tools.tool_definitions()

    @staticmethod
    def _lock_for(session_id: str) -> threading.Lock:
        with _LOCKS_GUARD:
            return _SESSION_LOCKS.setdefault(session_id, threading.Lock())

    def _call_claude(self, messages: list[dict], allow_tools: bool):
        try:
            return self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=build_system_prompt(),
                tools=self._tool_defs,
                # On the last allowed round, forbid tools so Claude must answer.
                tool_choice={"type": "auto" if allow_tools else "none"},
                messages=messages,
                output_config={"effort": EFFORT, "format": _answer_schema()},
                # Caches tools + system + history between rounds and turns (cheaper, faster).
                cache_control={"type": "ephemeral"},
                # If the model declines a request, the API retries it on a fallback model.
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise AgentError("Anthropic API key is missing or invalid (check .env)") from exc
        except anthropic.RateLimitError as exc:
            raise AgentError("Anthropic API rate limit reached, try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise AgentError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError("Could not reach the Anthropic API") from exc

    def chat(self, message: str, session_id: str | None = None) -> AgentResponse:
        session_id = session_id or uuid.uuid4().hex
        with self._lock_for(session_id):
            return self._chat(message, session_id)

    def _chat(self, message: str, session_id: str) -> AgentResponse:
        t0 = time.monotonic()
        messages = db.load_session(self.conn, session_id)
        messages.append({"role": "user", "content": message})
        calls: list[ToolCall] = []
        usage = {
            "input_tokens": 0,  # uncached input only
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        }

        for round_no in range(MAX_TOOL_ROUNDS + 1):
            response = self._call_claude(messages, allow_tools=round_no < MAX_TOOL_ROUNDS)
            for k in usage:
                usage[k] += getattr(response.usage, k, 0) or 0

            # Keep every block exactly as returned (thinking blocks included): the API requires
            # history to be passed back unchanged. Non-streaming fallbacks only happen before
            # any output, so there's never a partial turn to clean up.
            messages.append(
                {"role": "assistant", "content": [b.to_dict() for b in response.content]}
            )

            if response.stop_reason == "refusal":
                raise AgentError("The model declined to answer this request")
            if response.stop_reason == "max_tokens":
                raise AgentError("The answer was cut off (max_tokens); try a narrower question")
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                break  # end_turn: the final answer is in this response

            # Run every tool Claude asked for. All results go back in ONE user message.
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                t_tool = time.monotonic()
                text, is_error = tools.execute(self.conn, block.name, block.input)
                calls.append(
                    ToolCall(
                        name=block.name,
                        input=block.input,
                        ok=not is_error,
                        duration_ms=int((time.monotonic() - t_tool) * 1000),
                    )
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

        answer = self._parse_answer(response)
        warnings = check_grounding(answer, tool_output_texts(messages))
        # Save only after a valid answer, so a failed turn doesn't break the session's history.
        db.save_session(self.conn, session_id, messages)
        return AgentResponse(
            session_id=session_id,
            answer=answer,
            tool_calls=calls,
            grounding_warnings=warnings,
            model=response.model,
            usage=usage,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    @staticmethod
    def _parse_answer(response) -> AgentAnswer:
        texts = [b.text for b in response.content if b.type == "text"]
        if not texts:
            raise AgentError("The model returned no answer text")
        try:
            # The API enforces the schema; Pydantic validates it again on our side.
            return AgentAnswer.model_validate_json(texts[-1])
        except ValidationError as exc:
            raise AgentError(f"Answer did not match the schema: {exc}") from exc


def format_answer(resp: AgentResponse) -> str:
    """Human-readable rendering for the CLI."""
    a = resp.answer
    lines = [a.answer, ""]
    if a.ranking:
        lines += [f"  {r.rank}. {r.name:<15} {r.score:>5}  {r.note}" for r in a.ranking] + [""]
    if a.reasoning:
        lines += ["Reasoning:"] + [f"  - {r}" for r in a.reasoning]
    if a.assumptions:
        lines += ["Assumptions:"] + [f"  - {x}" for x in a.assumptions]
    lines.append(f"Confidence: {a.confidence} ({a.confidence_reason})")
    lines.append(
        f"[tools: {', '.join(c.name for c in resp.tool_calls) or 'none'} | "
        f"{resp.latency_ms / 1000:.1f}s | {sum(v for k, v in resp.usage.items() if 'input' in k)} in / "
        f"{resp.usage['output_tokens']} out tokens]"
    )
    for w in resp.grounding_warnings:
        lines.append(f"WARNING (grounding): {w}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Quick manual test: uv run python -m app.agent.agent "your question"
    import sys

    agent = WeatherRiskAgent()
    sid = None
    questions = [a for a in sys.argv[1:] if not a.startswith("--")] or [input("> ")]
    for q in questions:
        r = agent.chat(q, sid)
        sid = r.session_id
        print(f"\n### {q}\n{format_answer(r)}")
        if "--json" in sys.argv:
            print(json.dumps(r.answer.model_dump(), indent=2))
