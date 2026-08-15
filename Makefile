PY := .venv/bin/python
VENV := .venv/bin

.DEFAULT_GOAL := check
.PHONY: check all lint types test smoke cover install-hooks dev clean-cover

# The gate: fast, deterministic, safe to run on every commit (~7s).
check: lint types test

# Everything, including the suites that spawn real watchers (~15s).
all: lint types test smoke cover

lint:
	@$(VENV)/ruff check .

types:
	@$(VENV)/mypy folder_watch --ignore-missing-imports

test:
	@$(PY) -m unittest discover -s tests

# Spawns watchers and exercises FSEvents end to end. Kept out of `check`
# because it is timing-sensitive; a flaky commit gate gets disabled, and a
# disabled gate protects nothing.
smoke:
	@./bin/test-addons

cover:
	@$(VENV)/coverage run --source=folder_watch -m unittest discover -s tests >/dev/null
	@$(VENV)/coverage report

# Hooks live in the repo so they are versioned; .git/hooks is not.
install-hooks:
	@ln -sf ../../hooks/pre-commit .git/hooks/pre-commit
	@chmod +x hooks/pre-commit
	@echo "pre-commit hook installed -> hooks/pre-commit"

dev:
	@$(VENV)/pip install -q -e '.[dev]'
	@echo "dev dependencies installed"

clean-cover:
	@rm -f .coverage
