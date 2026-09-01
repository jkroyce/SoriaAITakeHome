"""Export the Claude Code transcript(s) for this project into the repo.

The take-home asks for "raw exports of the AI sessions used to design and generate
the project", so this ships both:
  - the raw .jsonl exactly as Claude Code wrote it (nothing removed but secrets)
  - a readable .md rendering, so a reviewer can actually read it

Deliberately NOT sanitized for content: wrong turns, corrections, and dead ends are
left in, because how the work was steered is part of what is being shown. Only
credentials are redacted.

Re-run this right before submitting to capture the full session.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "ai-sessions"
# This project's work is split across more than one transcript directory because the
# working directory changed mid-project: the design conversation (topic selection,
# source verification) was recorded under the home directory, the build under the
# project directory. Exporting only one silently drops half the deliverable -- and the
# brief asks for the sessions used to *design* as well as generate.
#
# This is an explicit allowlist, not a wildcard: other directories under
# ~/.claude/projects belong to unrelated work and are never touched.
PROJECT_DIRS = ["C--Users-jroyce", "c--Workspace-SoriaProblem"]
SRC_DIRS = [pathlib.Path.home() / ".claude" / "projects" / d for d in PROJECT_DIRS]

SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-REDACTED"),
    (re.compile(r"(ANTHROPIC_API_KEY\s*[=:]\s*)[^\s\"']+"), r"\1REDACTED"),
    (re.compile(r"(?i)(authorization\s*[=:]\s*bearer\s+)[^\s\"']+"), r"\1REDACTED"),
]


def scrub(text: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def render_markdown(rows: list[dict], session_id: str) -> str:
    lines = [f"# AI session transcript — `{session_id}`", ""]
    lines.append(f"_Rendered {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                 f"from {len(rows)} transcript events._")
    lines.append("")
    lines.append("Unedited apart from credential redaction. Wrong turns are left in on purpose.")
    lines.append("")
    lines.append("---")
    lines.append("")

    for row in rows:
        role = row.get("type")
        msg = row.get("message") or {}
        content = msg.get("content")
        if role not in ("user", "assistant") or content is None:
            continue
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        chunks: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text" and part.get("text", "").strip():
                chunks.append(part["text"].strip())
            elif ptype == "thinking" and part.get("thinking", "").strip():
                chunks.append("<details><summary>reasoning</summary>\n\n"
                              + part["thinking"].strip() + "\n\n</details>")
            elif ptype == "tool_use":
                name = part.get("name", "?")
                inp = json.dumps(part.get("input", {}), indent=2)[:4000]
                chunks.append(f"**→ tool: `{name}`**\n\n```json\n{inp}\n```")
            elif ptype == "tool_result":
                body = part.get("content")
                if isinstance(body, list):
                    body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                body = str(body or "")[:3000]
                chunks.append(f"**← result**\n\n```\n{body}\n```")
        if not chunks:
            continue
        lines.append(f"### {role}")
        lines.append("")
        lines.extend([c + "\n" for c in chunks])
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    files: list[pathlib.Path] = []
    for d in SRC_DIRS:
        if not d.exists():
            print(f"  . no transcripts in {d.name} (skipping)")
            continue
        found = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        print(f"  . {d.name}: {len(found)} transcript(s)")
        files.extend(found)
    if not files:
        print("! no .jsonl transcripts found in any project directory")
        return 1
    files.sort(key=lambda p: p.stat().st_mtime)

    for f in files:
        sid = f.stem
        raw = scrub(f.read_text(encoding="utf-8", errors="replace"))
        (OUT / f"{sid}.jsonl").write_text(raw, encoding="utf-8")

        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        (OUT / f"{sid}.md").write_text(render_markdown(rows, sid), encoding="utf-8")
        print(f"exported {sid}: {len(rows)} events, "
              f"{len(raw)//1024} KB raw -> ai-sessions/{sid}.{{jsonl,md}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
