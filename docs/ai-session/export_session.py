"""Turn Claude Code session logs (.jsonl) into a readable, redacted Markdown transcript.

Usage:
    python docs/ai-session/export_session.py OUT_DIR SESSION.jsonl [SESSION2.jsonl ...]
        [--subagent AGENT.jsonl ...] [--redact TERM ...]

Writes, for the given session files (in order, as consecutive parts of one session):
    OUT_DIR/transcript.md       every user message, assistant reply and tool call, in order
    OUT_DIR/session.jsonl       the same records as raw JSON (user/assistant only), redacted

What is left out, and why:
  - Harness bookkeeping records (API request blobs, file snapshots, cost state): not part of
    the conversation, large, and they repeat the system prompt.
  - <system-reminder> blocks injected into user turns: automatic context, not the user's words.
  - The model's private reasoning ("thinking" blocks): only the visible conversation is shown.
  - Long tool outputs are cut to their first lines in the Markdown (the raw JSONL keeps
    them whole).

Redaction runs on everything written: API keys, email addresses, and every --redact term
(e.g. your name and local username; matched case-insensitively, replaced with [redacted]).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Always-on rules. Personal terms (name, username, handle) are passed with --redact, so they
# are never written into this committed file.
REDACTIONS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_API_KEY]"),
    # Any email address except the public commit-attribution one.
    (
        re.compile(r"\b(?!noreply@anthropic\.com)[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+\.[A-Za-z.]{2,}"),
        "[email]",
    ),
]
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
MAX_RESULT_LINES, MAX_RESULT_CHARS = 40, 4000
MAX_INPUT_LINES = 80


def add_terms(terms: list[str]) -> None:
    # Longest first, so a full name is replaced before a shorter term it contains.
    for term in sorted(terms, key=len, reverse=True):
        REDACTIONS.append((re.compile(re.escape(term), re.IGNORECASE), "[redacted]"))


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(obj):
    """Redact every string inside a parsed record. Working on parsed text (not JSON text)
    matters: in JSON, "\\n@app.get" would look like an email address and get mangled."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj


def clip(text: str, max_lines: int, max_chars: int = 10**9) -> str:
    lines = text.splitlines()
    out = "\n".join(lines[:max_lines])[:max_chars]
    hidden = len(lines) - max_lines
    if hidden > 0 or len(text) > max_chars:
        out += f"\n… [{max(hidden, 0)} more lines cut for readability; full text in session.jsonl]"
    return out


def fence(text: str, lang: str = "") -> str:
    # Use a fence longer than any backtick run inside, so nested code blocks can't break it.
    ticks = "`" * max(3, max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def load_records(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") in ("user", "assistant"):
            out.append(redact_obj(rec))
    return out


def user_text(content) -> str:
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b.get("text", "") for b in content if b.get("type") == "text"]
    text = SYSTEM_REMINDER.sub("", "\n".join(parts)).strip()
    return text


def tool_result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        c = "\n".join(
            b.get("text", "[image]") if b.get("type") == "text" else f"[{b.get('type')}]" for b in c
        )
    return str(c or "").strip()


def render(records: list[dict], title: str) -> list[str]:
    md = [f"## {title}", ""]
    for rec in records:
        content = rec["message"]["content"]
        ts = rec.get("timestamp", "")[:19].replace("T", " ")
        if rec["type"] == "user":
            if rec.get("isCompactSummary"):
                md += [
                    f"### ⟳ Context summary ({ts} UTC)",
                    "",
                    (
                        "_The conversation ran out of context here; the agent continued from this "
                        "automatic summary of everything above._"
                    ),
                    "",
                    "<details><summary>Show summary</summary>",
                    "",
                    fence(user_text(content)),
                    "",
                    "</details>",
                    "",
                ]
                continue
            text = user_text(content)
            if text and rec.get("isMeta"):
                # Text the harness added on the user's behalf (e.g. a skill's instructions
                # being loaded): shown folded, since the user didn't type it.
                md += [
                    (
                        "<details><summary>📎 Context loaded automatically (not typed by the user)"
                        "</summary>"
                    ),
                    "",
                    fence(clip(text, MAX_RESULT_LINES, MAX_RESULT_CHARS)),
                    "",
                    "</details>",
                    "",
                ]
            elif text.startswith("<task-notification>"):
                # Sent by the app when a background command ends, not by the user.
                md += [f"_⚙️ System notification ({ts} UTC): a background task finished._", ""]
            elif text:
                md += [f"### 👤 User ({ts} UTC)", "", text, ""]
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result":
                        res = tool_result_text(b)
                        label = "Tool error" if b.get("is_error") else "Result"
                        md += [
                            f"<details><summary>{label}</summary>",
                            "",
                            fence(clip(res, MAX_RESULT_LINES, MAX_RESULT_CHARS) or "(empty)"),
                            "",
                            "</details>",
                            "",
                        ]
        else:
            for b in content:
                if b.get("type") == "text" and b["text"].strip():
                    md += [f"### 🤖 Assistant ({ts} UTC)", "", b["text"].strip(), ""]
                elif b.get("type") == "tool_use":
                    inp = b.get("input", {})
                    desc = inp.get("description") if isinstance(inp, dict) else None
                    head = f"**🔧 {b['name']}**" + (f" — {desc}" if desc else "")
                    if isinstance(inp, dict) and set(inp) <= {"command", "description", "timeout"}:
                        body = fence(clip(inp.get("command", ""), MAX_INPUT_LINES), "bash")
                    else:
                        body = fence(clip(json.dumps(inp, indent=2), MAX_INPUT_LINES), "json")
                    md += [head, "", body, ""]
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--subagent", action="append", type=Path, default=[])
    ap.add_argument("--redact", action="append", default=[], help="term to hide (repeatable)")
    args = ap.parse_args()
    add_terms(args.redact)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()  # a continued session repeats records of the one before it
    md = ["# AI agent session: Weather Risk Intelligence Agent", ""]
    raw: list[dict] = []
    for i, path in enumerate(args.sessions, 1):
        recs = [r for r in load_records(path) if r.get("uuid") not in seen]
        seen.update(r.get("uuid") for r in recs)
        md += render(recs, f"Part {i}")
        raw += recs
    for path in args.subagent:
        recs = load_records(path)
        md += render(recs, f"Sub-agent: {path.stem}")
        raw += recs

    for rec in raw:  # drop private reasoning from the raw log too
        content = rec["message"].get("content")
        if isinstance(content, list):
            rec["message"]["content"] = [b for b in content if b.get("type") != "thinking"]

    # Records were redacted when loaded; this second pass on the Markdown is a safety net.
    (args.out_dir / "transcript.md").write_text(redact("\n".join(md)) + "\n")
    with open(args.out_dir / "session.jsonl", "w") as f:
        for rec in raw:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"wrote {len(raw)} records to {args.out_dir}")


if __name__ == "__main__":
    main()
