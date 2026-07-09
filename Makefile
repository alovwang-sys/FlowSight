PYTHON ?= python3

.PHONY: check check-agent check-product test test-agent test-product validate-agent-system test-hooks test-validator gate-phase0 gate-phase1 gate-phase4 init guard

check: check-agent check-product

check-agent: validate-agent-system test-agent

check-product:
	@if [ ! -e flowsight ] && [ ! -e tests ] && [ ! -e pyproject.toml ] && [ ! -e ui ] && [ ! -e package.json ] && [ ! -e package-lock.json ]; then \
		echo "[pre-scaffold] no product source exists; this is NOT phase or release evidence"; \
	else \
		test -d flowsight && test -d tests && test -f pyproject.toml || { echo "partial product scaffold: flowsight/, tests/, and pyproject.toml are all required" >&2; exit 1; }; \
		$(PYTHON) -m ruff format --check .; \
		$(PYTHON) -m ruff check .; \
		$(PYTHON) -m mypy flowsight; \
		if [ -e ui ] || [ -e package.json ] || [ -e package-lock.json ]; then \
			test -d ui && test -f package.json && test -f package-lock.json || { echo "frontend scaffold requires ui/, package.json, and package-lock.json" >&2; exit 1; }; \
			npm run check && npm test && npm run build; \
		fi; \
		$(PYTHON) -m pytest; \
		if [ -e ui ] || [ -e package.json ] || [ -e package-lock.json ]; then \
			test -f tests/packaging/test_wheel_ui.py || { echo "frontend scaffold requires clean-wheel UI test" >&2; exit 1; }; \
			workspace=$$(pwd); wheel_tmp=$$(mktemp -d); trap 'rm -rf "$$wheel_tmp"' EXIT; \
			$(PYTHON) -m build --wheel --outdir "$$wheel_tmp/dist"; \
			$(PYTHON) -m venv "$$wheel_tmp/venv"; \
			wheel=$$(find "$$wheel_tmp/dist" -name '*.whl' -print -quit); \
			"$$wheel_tmp/venv/bin/python" -m pip install "$$wheel" pytest; \
			mkdir -p "$$wheel_tmp/test"; \
			cp "$$workspace/tests/packaging/test_wheel_ui.py" "$$wheel_tmp/test/test_wheel_ui.py"; \
			(cd "$$wheel_tmp" && PYTHONPATH= PYTHONNOUSERSITE=1 "$$wheel_tmp/venv/bin/python" -m pytest --rootdir="$$wheel_tmp" --import-mode=importlib "$$wheel_tmp/test/test_wheel_ui.py"); \
		fi; \
	fi

test: test-agent test-product

test-agent: test-hooks test-validator

test-product:
	@if [ ! -e flowsight ] && [ ! -e tests ] && [ ! -e pyproject.toml ] && [ ! -e ui ] && [ ! -e package.json ] && [ ! -e package-lock.json ]; then \
		echo "[pre-scaffold] product tests unavailable"; \
	else \
		test -d flowsight && test -d tests && test -f pyproject.toml || { echo "partial product scaffold: product tests cannot run" >&2; exit 1; }; \
		$(PYTHON) -m pytest; \
		if [ -e ui ] || [ -e package.json ] || [ -e package-lock.json ]; then \
			test -d ui && test -f package.json && test -f package-lock.json || { echo "frontend scaffold requires ui/, package.json, and package-lock.json" >&2; exit 1; }; \
			npm test; \
		fi; \
	fi

validate-agent-system:
	$(PYTHON) scripts/validate_agent_system.py

test-hooks:
	$(PYTHON) scripts/agent/test_pre_bash_guard.py

test-validator:
	$(PYTHON) scripts/agent/test_validate_agent_system.py

gate-phase0: check
	$(PYTHON) scripts/validate_agent_system.py --gate phase0-sustained

gate-phase1: check
	$(PYTHON) scripts/validate_agent_system.py --gate phase1-runtime-ingest

gate-phase4: check
	$(PYTHON) scripts/validate_agent_system.py --gate phase4-tracepoint

# One-time per clone: route git hooks through the versioned .githooks dir so the
# shared guard runs on commit for every tool (Claude Code, Codex, humans).
init:
	git config core.hooksPath .githooks
	@echo "core.hooksPath -> .githooks (shared pre-commit guard active in this clone)"

# Manually check whether a shell command would be blocked by the shared guard.
# Usage: make guard CMD='git reset --hard'
guard:
	@printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$(CMD)" | \
		$(PYTHON) scripts/agent/pre_bash_guard.py; \
		echo "guard: a deny above means blocked; no output means allowed"
