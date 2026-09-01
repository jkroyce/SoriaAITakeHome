# Thin delegation to run.py, which is the real task runner.
# `make` is not available on the Windows machine this was built on, so the logic
# lives in Python and works everywhere; this file exists so `make demo` behaves as
# expected for anyone on macOS or Linux.
PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python)

.PHONY: setup fetch test contract demo live tick ui chat sessions cost
setup fetch test contract demo live tick ui chat sessions cost:
	@$(PY) run.py $@
