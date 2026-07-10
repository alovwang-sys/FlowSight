# Task: Disable Background Git Maintenance in Test Fixtures

## Task Metadata

```yaml
task_id: TOOL-003
release: v1
task_type: tooling
status: in_progress
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

- [ ] Every `TemporaryGitRepository` disables `maintenance.auto` and legacy `gc.auto` before creating commits.
- [ ] A regression test proves both fixture-local settings are present.
- [ ] The allowlist suite and full `make check` pass without suppressing cleanup failures.
- [ ] Git configuration is scoped to temporary fixture repositories only.

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
- TBD

Adversarial Reviewer:
- Reviewer 1: TBD
- Reviewer 2: waived: one-file deterministic test-fixture correction after exact CI traceback and official Git configuration confirmation

Fixer:
- TBD

Quality Governor:
- TBD

## Verifier Evidence

- Command: `python3 scripts/agent/test_check_staged_files.py && make check`
- Result: TBD
- Notes: TBD

## Failure Queue Items

- none
