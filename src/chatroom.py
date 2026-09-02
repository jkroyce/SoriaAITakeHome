"""A shared, append-only log where every agent's activity is visible in one file.

Why this exists: parallel agents are otherwise opaque. You can see the final diff,
but not who was spawned, what they were told to do, what they got stuck on, or which
decisions they made along the way. `ai-sessions/chatroom.md` makes all of that visible in one
human-readable file, live, while the work is happening.

THE WORKTREE PROBLEM (and why this is not just `open("ai-sessions/chatroom.md", "a")`):
each build agent runs in its own git worktree, which is a separate checkout with its
own copy of every tracked file. A relative path would give each agent a private
chatroom nobody else can see -- the exact opposite of the point. So the path is
resolved via `git rev-parse --git-common-dir`, which from inside any worktree points
at the MAIN repository's .git directory. Its parent is the main working tree, and
that is where the single shared chatroom lives.

Used by both managers:
  * the build-time manager posts SPAWN / MERGE events as it creates agents
  * the runtime manager posts DISPATCH / ESCALATE / FLAG events as it routes work
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

#: Lives with the other build-time AI records rather than at the repo root. It is
#: evidence of how this was built, not documentation anyone runs the system from, and
#: the root is what a reviewer reads first.
FILENAME = pathlib.Path("ai-sessions") / "chatroom.md"
_MAX_APPEND_RETRIES = 8

KINDS = {
    "spawn":    "SPAWN",
    "done":     "DONE",
    "note":     "note",
    "ask":      "ASK",
    "answer":   "answer",
    "dispatch": "DISPATCH",
    "escalate": "ESCALATE",
    "flag":     "FLAG",
    "learn":    "LEARN",
    "error":    "ERROR",
}


def _main_worktree_root() -> pathlib.Path:
    """The main repo root, even when called from inside a linked worktree."""
    env = os.environ.get("CHATROOM_ROOT")
    if env:
        return pathlib.Path(env)
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
            cwd=pathlib.Path(__file__).resolve().parent,
        )
        if common.returncode == 0:
            git_dir = pathlib.Path(common.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = (pathlib.Path(__file__).resolve().parent / git_dir).resolve()
            # <root>/.git -> <root>
            return git_dir.parent
    except Exception:
        pass
    return pathlib.Path(__file__).resolve().parent.parent


def path() -> pathlib.Path:
    return _main_worktree_root() / FILENAME


def _append(line: str) -> None:
    """Append one line, tolerating concurrent writers.

    Several agents may append at the same moment. A single small write in append
    mode is atomic enough in practice; the retry covers Windows sharing violations.
    """
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_MAX_APPEND_RETRIES):
        try:
            with open(p, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line if line.endswith("\n") else line + "\n")
            return
        except (PermissionError, OSError):
            time.sleep(0.05 * (attempt + 1))
    sys.stderr.write(f"! chatroom append failed after retries: {line[:80]}\n")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def ensure_header() -> pathlib.Path:
    p = path()
    if not p.exists() or p.stat().st_size == 0:
        p.write_text(
            "# Agent chatroom\n\n"
            "Append-only activity log, shared by every agent across every worktree.\n"
            "Newest entries at the bottom. Written by `src/chatroom.py`.\n\n"
            "| kind | meaning |\n|---|---|\n"
            "| `SPAWN` | the build manager created an agent |\n"
            "| `DONE` | an agent finished its section |\n"
            "| `ASK` / `answer` | a question between agents |\n"
            "| `DISPATCH` | the runtime manager routed a work item |\n"
            "| `ESCALATE` | low confidence, retried on a stronger model |\n"
            "| `FLAG` | still uncertain, sent to human review |\n"
            "| `LEARN` | an agent wrote itself a new skill rule |\n\n"
            "---\n\n",
            encoding="utf-8",
        )
    return p


def post(sender: str, text: str, to: str = "all", kind: str = "note") -> None:
    """Post one message. This is the whole API."""
    ensure_header()
    label = KINDS.get(kind, kind.upper())
    _append(f"`{_stamp()}` **{sender} → {to}** · {label} · {text}")


def spawn(agent: str, branch: str, owns: str, task: str, sender: str = "builder") -> None:
    post(sender,
         f"created `{agent}` on branch `{branch}` — owns `{owns}` — {task}",
         to=agent, kind="spawn")


def done(agent: str, summary: str, to: str = "builder") -> None:
    post(agent, summary, to=to, kind="done")


def tail(n: int = 30) -> str:
    p = path()
    if not p.exists():
        return "(chatroom empty)"
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("`")]
    return "\n".join(lines[-n:]) or "(no messages yet)"


if __name__ == "__main__":
    # The Windows console defaults to cp1252, which cannot encode the arrows used in
    # the log format. Force UTF-8 on the way out rather than degrading the format.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tail"
    if cmd == "tail":
        print(tail(int(sys.argv[2]) if len(sys.argv) > 2 else 30))
    elif cmd == "where":
        print(path())
    elif cmd == "post":
        post(sys.argv[2], " ".join(sys.argv[4:]), to=sys.argv[3])
    else:
        print(__doc__)
