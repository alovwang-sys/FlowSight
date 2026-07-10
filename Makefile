PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# A frontend scaffold exists when either root manifest exists or Git can see a
# tracked/non-ignored untracked path under ui/. Outside a usable Git worktree,
# fall back to the physical ui path so source archives fail closed; ignored
# dependency/tool caches remain invisible in a normal Git checkout.
FRONTEND_PRESENT = test -e package.json || test -e package-lock.json || { \
	if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		if visible_ui=$$(git ls-files --cached --others --exclude-standard -- ui 2>/dev/null); then test -n "$$visible_ui"; else test -e ui; fi; \
	else \
		test -e ui; \
	fi; \
}

.PHONY: check check-fast check-agent check-product check-product-fast check-staged-files test test-agent test-product validate-agent-system test-hooks test-hooks-fast test-formatter test-allowlist test-product-detection test-validator gate-phase0 gate-phase1 gate-phase4 init guard

check: check-agent check-product

# Deterministic commit-time subset: canonical validation, cheap guard/formatter
# smoke tests, and product static checks. Full integration tests, product tests,
# builds, and clean-wheel verification remain in `make check` and CI.
check-fast: validate-agent-system test-hooks-fast test-formatter test-validator check-product-fast

check-agent: validate-agent-system test-agent

check-product:
	@if [ ! -e flowsight ] && [ ! -e tests ] && [ ! -e pyproject.toml ] && ! { $(FRONTEND_PRESENT); }; then \
		echo "[pre-scaffold] no product source exists; this is NOT phase or release evidence"; \
	else \
		has_python=0; has_frontend=0; \
		if [ -e tests ] || [ -e pyproject.toml ] || find flowsight -type f -name '*.py' -print -quit 2>/dev/null | grep -q .; then has_python=1; fi; \
		if { $(FRONTEND_PRESENT); }; then has_frontend=1; fi; \
		if [ "$$has_python" -eq 1 ]; then \
			test -d flowsight && test -d tests && test -f pyproject.toml || { echo "partial Python scaffold: flowsight/, tests/, and pyproject.toml are all required" >&2; exit 1; }; \
			test -d examples || { echo "Python scaffold requires examples/" >&2; exit 1; }; \
			$(PYTHON) -m ruff format --check flowsight examples tests; \
			$(PYTHON) -m ruff check flowsight examples tests; \
			$(PYTHON) -m mypy flowsight; \
		fi; \
		if [ "$$has_frontend" -eq 1 ]; then \
			test -d ui && test -f package.json && test -f package-lock.json || { echo "frontend scaffold requires ui/, package.json, and package-lock.json" >&2; exit 1; }; \
			npm run check && npm test && npm run build; \
		fi; \
		if [ "$$has_python" -eq 1 ]; then $(PYTHON) -m pytest; fi; \
		if [ "$$has_python" -eq 1 ] && [ "$$has_frontend" -eq 1 ]; then \
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
		if [ "$$has_python" -eq 0 ] || [ "$$has_frontend" -eq 0 ]; then echo "[partial-scaffold] verified present side only; this is NOT Phase 0 evidence"; fi; \
	fi

check-product-fast:
	@if [ ! -e flowsight ] && [ ! -e tests ] && [ ! -e pyproject.toml ] && ! { $(FRONTEND_PRESENT); }; then \
		echo "[pre-scaffold] no product source exists; fast check is agent-system evidence only"; \
	else \
		if [ -e tests ] || [ -e pyproject.toml ] || find flowsight -type f -name '*.py' -print -quit 2>/dev/null | grep -q .; then \
			test -d flowsight && test -d tests && test -f pyproject.toml || { echo "partial Python scaffold: flowsight/, tests/, and pyproject.toml are all required" >&2; exit 1; }; \
			test -d examples || { echo "Python scaffold requires examples/" >&2; exit 1; }; \
			$(PYTHON) -m ruff format --check flowsight examples tests; \
			$(PYTHON) -m ruff check flowsight examples tests; \
			$(PYTHON) -m mypy flowsight; \
		fi; \
		if { $(FRONTEND_PRESENT); }; then \
			test -d ui && test -f package.json && test -f package-lock.json || { echo "frontend scaffold requires ui/, package.json, and package-lock.json" >&2; exit 1; }; \
			npm run check; \
		fi; \
	fi

test: test-agent test-product

test-agent: test-hooks test-formatter test-allowlist test-product-detection test-validator

test-product:
	@if [ ! -e flowsight ] && [ ! -e tests ] && [ ! -e pyproject.toml ] && ! { $(FRONTEND_PRESENT); }; then \
		echo "[pre-scaffold] product tests unavailable"; \
	else \
		has_python=0; has_frontend=0; \
		if [ -e tests ] || [ -e pyproject.toml ] || find flowsight -type f -name '*.py' -print -quit 2>/dev/null | grep -q .; then has_python=1; fi; \
		if { $(FRONTEND_PRESENT); }; then has_frontend=1; fi; \
		if [ "$$has_python" -eq 1 ]; then \
			test -d flowsight && test -d tests && test -f pyproject.toml || { echo "partial Python scaffold: product tests cannot run" >&2; exit 1; }; \
			$(PYTHON) -m pytest; \
		fi; \
		if [ "$$has_frontend" -eq 1 ]; then \
			test -d ui && test -f package.json && test -f package-lock.json || { echo "frontend scaffold requires ui/, package.json, and package-lock.json" >&2; exit 1; }; \
			npm test; \
		fi; \
	fi

validate-agent-system:
	$(PYTHON) scripts/validate_agent_system.py

test-hooks:
	$(PYTHON) scripts/agent/test_pre_bash_guard.py

test-hooks-fast:
	$(PYTHON) scripts/agent/test_pre_bash_guard.py --smoke

test-formatter:
	$(PYTHON) scripts/agent/test_post_edit_format.py

test-allowlist:
	$(PYTHON) scripts/agent/test_check_staged_files.py

test-product-detection:
	$(PYTHON) scripts/agent/test_product_detection.py

test-validator:
	$(PYTHON) scripts/agent/test_validate_agent_system.py

check-staged-files:
	$(PYTHON) scripts/agent/check_staged_files.py

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
# Stdin avoids evaluating or re-quoting the candidate command in this Makefile.
# Usage: printf '%s\n' 'git reset --hard' | make guard
guard:
	@$(PYTHON) scripts/agent/run_guard.py
