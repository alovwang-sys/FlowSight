# Task: Disable Background Git Maintenance in Test Fixtures

## Task Metadata

```yaml
task_id: TOOL-003
release: v1
task_type: tooling
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [TOOL-002]
requires_gates: []
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

TOOL-003

## Phase

Phase 0

## Goal

Prevent detached Git auto-maintenance from racing `TemporaryDirectory` cleanup in real-repository allowlist tests.

## Context

- GitHub Actions run 29062053720 failed on Ubuntu/Python 3.12 after all guard checks passed because a temporary fixture's `.git` directory was repopulated during `shutil.rmtree`.
- Git documents that `maintenance.auto` defaults to true and `maintenance.autoDetach` defaults to detached/background execution.
- This is test-fixture determinism only; it must not change production checks or hide cleanup errors.

## Related Fact IDs

- FS-023

## Allowed Files

- `scripts/agent/test_check_staged_files.py`
- `tasks/tooling/003-disable-fixture-git-maintenance.md`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `scripts/agent/test_check_staged_files.py`

## Forbidden

- Do not ignore `TemporaryDirectory` cleanup errors or add retry sleeps.
- Do not change staged-file checker behavior.
- Do not change Git configuration outside each temporary fixture repository.
- Do not modify product or frontend source.

## Acceptance Criteria

- [x] Every `TemporaryGitRepository` disables `maintenance.auto` and legacy `gc.auto` before creating commits.
- [x] A regression test proves both fixture-local settings are present.
- [x] The allowlist suite and full `make check` pass without suppressing cleanup failures.
- [x] Git configuration is scoped to temporary fixture repositories only.

## No-Test Reason

N/A

## Verification

Run:

```sh
python3 scripts/agent/test_check_staged_files.py
make check
```

Expected result:

```text
temporary Git fixtures cannot launch detached auto-maintenance; all shared checks pass
```

## Risks

- Setting only `gc.auto=0` would miss the newer maintenance framework.
- Hiding cleanup errors would turn the flake into leaked temporary state.

## Reviewer Focus

- Are both settings written before any commit can trigger maintenance?
- Does the test remain fixture-scoped and preserve strict cleanup?

## Role Outputs

Implementer:
- Disabled modern Git auto-maintenance and legacy auto-GC immediately after each temporary repository is initialized, and added local-scope assertions without weakening strict cleanup.

Adversarial Reviewer:
- Reviewer 1: Confirmed the CI traceback matches a detached Git maintenance cleanup race, required `--local` assertions to avoid inherited-config false confidence, and reported PASS after the correction with 24/24 tests.
- Reviewer 2: waived: one-file deterministic test-fixture correction after exact CI traceback and official Git configuration confirmation

Fixer:
- Tightened both configuration assertions to `git config --local --get`; no retries, sleeps, cleanup suppression, or production checker changes were accepted.

Quality Governor:
- PASS: verified the one-file allowlist, fixture-only config writes before any commit, valid reviewer waiver, strict cleanup, 24/24 tests, and absence of product, gate, or checker behavior changes.

## Verifier Evidence

- Command: `python3 scripts/agent/test_check_staged_files.py && make check`
- Result: PASS
- Notes: Independent verifier reproduced Git-index tree `02c74370b056887aa8e29a5614962383474674d8` with exactly one staged path. The allowlist suite passed 24/24 in 10.45s; `make check` passed in about 27s with guard 158/80, formatter 4/4, product detection 5/5, validator 11/11, Ruff, mypy, and Python 4/4. The real index-snapshot pre-commit passed in about 6s with smoke guard 17/7. Static inspection proved both settings are written after fixture init and before any commit, asserted with `--local --get`, while strict `TemporaryDirectory` cleanup remains unchanged. GitHub Actions run 29062640572 passed all four Ubuntu/macOS × CPython 3.12/3.13 jobs for implementation commit `0e899f3e5783310c9109c46b4196d6413ca77251`, including the Ubuntu/Python 3.12 combination that exposed the cleanup race. Output explicitly remained partial-scaffold evidence and opened no gate.

## Failure Queue Items

- none
