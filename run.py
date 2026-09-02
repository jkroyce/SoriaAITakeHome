#!/usr/bin/env python
"""Task runner. `python run.py <task>`.

Python rather than a Makefile because `make` is not present on the Windows machine
this was built on, and a task runner you cannot run is just documentation. The
Makefile delegates here so `make demo` still works for anyone on macOS or Linux --
one implementation, two entry points.

    python run.py demo      # rebuild everything from the committed cache, no API key
    python run.py trial     # prove it live on your own key, capped at $0.25
    python run.py tick      # one refresh cycle
    python run.py ui        # Streamlit
    python run.py test      # test suite
    python run.py chat      # tail the agent chatroom
    python run.py cost      # what the cache holds and what it saved
    python run.py golden    # score extraction against the golden fixtures
"""
from __future__ import annotations

import os
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
    """Run a child task, streaming its output as it happens.

    PYTHONUNBUFFERED matters more than it looks: a child writing to a pipe
    block-buffers stdout, so a long task (`live` takes about an hour over 50
    documents) prints nothing at all until it exits. That is indistinguishable
    from a hang, and on a paid run it hides how much has been spent so far.
    """
    print(f"  $ {' '.join(str(a) for a in args)}", flush=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    return subprocess.run(args, cwd=ROOT, env=env, **kw).returncode


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
    """The reviewer's path: the whole pipeline from committed inputs. No key, $0.

    `--rebuild` deletes the database first, on purpose. Replaying onto a warm database
    proves nothing -- every stage correctly finds no work and the run reports zeroes.
    Starting cold makes the model responses in cache/llm/ actually do the work, so the
    1,182 awards and 1,128 contracts you end up with are reconstructed rather than
    read back, and the $0.00 in the footer is the claim being demonstrated.
    """
    if not (SRC / "manager.py").exists():
        return _missing("src/manager.py", "demo")
    return sh([PY, "-m", "src.manager", "tick", "--offline", "--rebuild"])


def task_trial() -> int:
    """Prove it is real, on your own key, for about a nickel.

    The honest question about a committed cache is whether it is a fixture -- whether
    the thing would still work against text it has never seen. This answers it: fetch
    today's announcements, take ONE document that is genuinely not in the cache, and
    run it live. Capped at $0.25 and one document, so a reviewer can satisfy their
    curiosity without thinking about the bill.

    Needs ANTHROPIC_API_KEY. Everything else the repo does runs without one.
    """
    if not (SRC / "manager.py").exists():
        return _missing("src/manager.py", "trial")
    if not (os.environ.get("ANTHROPIC_API_KEY") or (ROOT / ".env").exists()):
        print("trial needs an API key. Set ANTHROPIC_API_KEY, or put it in .env "
              "(gitignored):")
        print('    echo "ANTHROPIC_API_KEY=sk-ant-..." > .env')
        print("Nothing was called and nothing was spent.")
        return 2
    print("Fetching the latest index page, then processing ONE new document live.")
    print("Capped at $0.25. Anything already cached is free and will not be re-called.")
    rc = sh([PY, str(SRC / "fetch.py"), "1"])
    if rc != 0:
        return rc
    return sh([PY, "-m", "src.manager", "tick", "--live",
               "--limit", "1", "--max-spend", "0.25"])


def task_live() -> int:
    """The full pipeline, permitted to call the API for genuinely new inputs only."""
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
