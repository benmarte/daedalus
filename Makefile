# Daedalus — developer convenience targets.
#
# Most of the project's tests run as plain pytest, but the end-to-end
# regression suite (issue #898/#903) is broken out into its own target so it
# can be driven identically from a developer machine and from the nightly CI
# schedule (.github/workflows/e2e-nightly.yml).
#
# Usage:
#   make help        # list targets
#   make install     # install runtime + test deps
#   make test        # full unit/integration suite
#   make lint        # ruff lint (changed-file friendly)
#   make e2e         # offline E2E regression suite (seeds an issue, drives
#                    # the full validator->...->docs pipeline, reports pass/fail)
#   make e2e-live    # live smoke against the REAL dispatcher (needs GITHUB_TOKEN)

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip

# The offline E2E regression suite. test_e2e_full_pipeline seeds one controlled
# issue and walks it through all seven stages; test_e2e_smoke validates a fresh
# Hermes install; test_pipeline_scenarios covers happy/block/escalate slices.
E2E_TESTS := tests/test_e2e_full_pipeline.py \
             tests/test_e2e_multi_tick.py \
             tests/test_e2e_smoke.py \
             tests/test_dispatch_selftest.py \
             tests/test_pipeline_scenarios.py

.DEFAULT_GOAL := help

.PHONY: help install test lint e2e e2e-live

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime + test dependencies
	$(PIP) install --quiet pyyaml fastapi pytest httpx ruff

test: ## Run the full unit/integration suite
	$(PYTHON) tests/test_daedalus.py
	$(PYTHON) -m pytest tests/ -q

lint: ## Lint changed Python files (falls back to the whole repo)
	@files=$$(git diff --name-only --diff-filter=ACM origin/dev...HEAD -- '*.py' 2>/dev/null); \
	if [ -n "$$files" ]; then \
		echo "ruff: $$files"; ruff check $$files; \
	else \
		echo "ruff: no changed Python files"; \
	fi

e2e: ## Run the offline E2E regression suite (seed issue -> full pipeline -> pass/fail)
	@echo "=== Daedalus E2E regression suite ==="
	@echo "--- Seeding a controlled issue and driving the full pipeline ---"
	$(PYTHON) -m pytest $(E2E_TESTS) -v
	@echo "--- Standalone smoke runner (dual-mode parity check) ---"
	$(PYTHON) tests/test_e2e_smoke.py
	@echo "--- Dispatcher --self-test (offline, no real GitHub) ---"
	$(PYTHON) scripts/daedalus_dispatch.py --self-test
	@echo "=== E2E suite PASSED ==="

e2e-live: ## Run the live smoke test against the REAL dispatcher (requires GITHUB_TOKEN)
	@if [ -z "$$GITHUB_TOKEN" ]; then \
		echo "FATAL: GITHUB_TOKEN not set — required for the live smoke test."; \
		exit 1; \
	fi
	bash scripts/e2e_smoke_test.sh
