#!/usr/bin/env python
"""Task runner. `python run.py <task>`.

Python rather than a Makefile because `make` is not present on the Windows machine
this was built on, and a task runner you cannot run is just documentation. The
Makefile delegates here so `make demo` still works for anyone on macOS or Linux --
one implementation, two entry points.

    python run.py demo      # replay everything from the committed cache, no API key
    python run.py tick      # one refresh cycle
    python run.py ui        # Streamlit
    python run.py test      # test suite
    python run.py chat      # tail the agent chatroom
    python run.py cost      # what the cache holds and what it saved
    python run.py golden    # score extraction against the golden fixtures
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "src"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = ROOT / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else sys.executable


def sh(args: list[str], **kw) -> int:
    print(f"  $ {' '.join(str(a) for a in args)}")
    return subprocess.run(args, cwd=ROOT, **kw).returncode


def _missing(module: str, task: str) -> int:
    """Report an unbuilt module honestly instead of failing with a traceback."""
    print(f"! {module} does not exist yet, so `{task}` cannot run.")
    print(f"  It is built in a later wave. `python run.py test` works now.")
    return 1


def task_setup() -> int:
    rc = sh([PY, "-m", "pip", "install", "-q", "-r", "requirements.txt"])
    print("dependencies installed" if rc == 0 else "! install failed")
    return rc


def task_fetch(pages: str = "5") -> int:
    """Re-acquire source HTML. Deterministic and free -- no model involved."""
    return sh([PY, str(SRC / "fetch.py"), pages])


def task_test() -> int:
    return sh([PY, "-m", "pytest", "tests/", "-q"])


def task_contract() -> int:
    """Print the frozen contract: tables, columns, and which fields a model fills."""
    return sh([PY, str(SRC / "schemas.py")])


def task_demo() -> int:
    """The reviewer's two-minute path: full pipeline, from cache, no API key, $0.

    Every model response is cached by input hash and committed, so this replays the
    agent pipeline exactly without calling anything.
    """
    if not (SRC / "manager.py").exists():
        return _missing("src/manager.py", "demo")
    return sh([PY, "-m", "src.manager", "tick", "--offline"])


def task_live() -> int:
    """Same pipeline, permitted to call the API for genuinely new inputs only."""
    if not (SRC / "manager.py").exists():
        return _missing("src/manager.py", "live")
    return sh([PY, "-m", "src.manager", "tick", "--live", "--max-spend", "5.00"])


def task_tick() -> int:
    if not (SRC / "manager.py").exists():
        return _missing("src/manager.py", "tick")
    return sh([PY, "-m", "src.manager", "tick", "--dry-run"])


def task_ui() -> int:
    if not (ROOT / "app.py").exists():
        return _missing("app.py", "ui")
    return sh([PY, "-m", "streamlit", "run", "app.py"])


def task_chat(n: str = "40") -> int:
    """Tail the shared agent chatroom."""
    return sh([PY, str(SRC / "chatroom.py"), "tail", n])


def task_sessions() -> int:
    """Re-export the AI session transcripts. Run before submitting."""
    return sh([PY, "scripts/export_sessions.py"])


def task_usage() -> int:
    """Per-agent token usage and spike detection, build-time and runtime."""
    return sh([PY, "scripts/agent_usage.py"])


def task_golden(*args: str) -> int:
    """Score extraction against the hand-verified fixtures. Gates skill promotion."""
    runner = ROOT / "tests" / "golden" / "runner.py"
    if not runner.exists():
        return _missing("tests/golden/runner.py", "golden")
    # Cache mode by default: BLOCKED (exit 2) until the paid pass runs, which is
    # honest -- a blocked case is never counted as a pass.
    return sh([PY, str(runner), *args])


def task_cost() -> int:
    """What the committed cache holds, and what replaying it costs (nothing)."""
    sys.path.insert(0, str(SRC))
    try:
        import llm
    except Exception as e:  # pragma: no cover
        print(f"! could not import llm: {e}")
        return 1
    st = llm.cache_stats()
    print(f"cached model responses : {st['entries']}")
    print(f"tokens already paid for: {st['input_tokens']:,} in / {st['output_tokens']:,} out")
    hay = llm.PRICING["claude-haiku-4-5"]
    saved = st["input_tokens"] / 1e6 * hay["in"] + st["output_tokens"] / 1e6 * hay["out"]
    print(f"replaying this cache costs $0.00 (would be ${saved:.4f} at Haiku rates)")
    return 0


TASKS = {n[5:]: f for n, f in sorted(globals().items()) if n.startswith("task_")}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        print("tasks:")
        for name, fn in TASKS.items():
            doc = (fn.__doc__ or "").strip().splitlines()
            print(f"  {name:10s} {doc[0] if doc else ''}")
        return 0
    name, *rest = sys.argv[1:]
    if name not in TASKS:
        print(f"! unknown task {name!r}. Known: {', '.join(TASKS)}")
        return 1
    return TASKS[name](*rest)


if __name__ == "__main__":
    raise SystemExit(main())
