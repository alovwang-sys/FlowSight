# Task: Harden Agent Enforcement Boundaries

## Task Metadata

```yaml
task_id: TOOL-001
release: v1
task_type: tooling
status: review
primary_phase: phase0
impacted_phases: []
depends_on: []
requires_gates: []
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

TOOL-001

## Phase

Phase 0

## Goal

Close the practical guard bypasses found during adversarial testing, enforce staged files against the active task allowlist, keep pre-commit fast, and leave full product verification to CI and explicit `make check`.

## Context

- Source of truth:
  - `AGENTS.md`
  - `docs/agent-operating-system.md`
  - `docs/agent-tooling.md`
  - `docs/agent-rule-authoring.md`
- This is control-plane hardening; it must not add FlowSight product behavior.

## Related Fact IDs

- FS-023

## Allowed Files

- `scripts/agent/**`
- `.claude/hooks/**`
- `.githooks/**`
- `AGENTS.md`
- `Makefile`
- `docs/agent-operating-system.md`
- `docs/agent-tooling.md`
- `docs/agent-rule-authoring.md`
- `tasks/tooling/001-enforcement-hardening.md`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `scripts/agent/pre_bash_guard.py`
- `scripts/agent/test_pre_bash_guard.py`
- `scripts/agent/run_guard.py`
- `scripts/agent/check_staged_files.py`
- `scripts/agent/test_check_staged_files.py`
- `scripts/agent/post_edit_format.py`
- `scripts/agent/test_post_edit_format.py`
- `.githooks/pre-commit`
- `AGENTS.md`
- `Makefile`
- `docs/agent-operating-system.md`
- `docs/agent-tooling.md`
- `docs/agent-rule-authoring.md`

## Forbidden

- Do not claim that local hooks are impossible to bypass.
- Do not make pre-commit run the clean-wheel or full frontend build.
- Do not weaken CI `make check`.
- Do not modify FlowSight product source or frontend source.

## Acceptance Criteria

- [x] Guard rejects shell wrappers, absolute Git paths, multiline destructive commands, `commit --no-verify`, destructive checkout/restore paths, public-bind environment prefixes, and broad repository/parent `rm -rf` targets.
- [x] Guard tests include safe branch switching, safe loopback binding, and narrow temporary-directory deletion to control false positives.
- [x] A staged-file checker rejects zero/multiple active tasks where product changes are staged and rejects staged paths outside the single active task's `Allowed Files`.
- [x] Rename/delete/control-plane/current-task-card behavior is covered by tests in temporary Git repositories.
- [x] Pre-commit runs the staged checker and fast deterministic subset from a temporary Git-index snapshot; unstaged rewrites cannot mask unsafe staged content, while CI and explicit `make check` retain full Python/frontend/wheel verification.
- [x] Python formatting only uses the project virtualenv's Ruff module, and frontend formatting only uses the repository-local Prettier binary; neither falls back to arbitrary global PATH tools. Dependency manifests pin those tools in their owning scaffold tasks.
- [x] Documentation states that hooks remain bypassable by humans and that protected-branch CI is the merge guarantee.

## No-Test Reason

N/A

## Verification

Run:

```sh
python3 scripts/agent/test_pre_bash_guard.py
python3 scripts/agent/test_check_staged_files.py
make check
```

Expected result:

```text
guard and staged allowlist tests pass; shared checks pass
```

## Risks

- Shell parsing can create false confidence if wrappers or separators are missed.
- An over-broad staged allowlist can block legitimate task-card evidence updates.
- Slow hooks encourage bypass instead of compliance.

## Reviewer Focus

- Can a destructive equivalent still bypass normalization?
- Can a staged rename/delete escape the allowlist?
- Is the hook honest about its bypassability and performance boundary?

## Role Outputs

Implementer:
- Added recursive command normalization and regression coverage for destructive Git/filesystem forms, opaque shell wrappers, Git long-option abbreviations, and non-loopback binds; added index-only task enforcement, snapshot pre-commit checks, deterministic formatter selection, and fast/full verification separation.

Adversarial Reviewer:
- Reviewer 1: Reproduced initial hook/config, shell-wrapper, deletion, public-bind, task-identity, index/worktree, formatter, and performance gaps; every concrete P0/P1 finding was accepted and fixed.
- Reviewer 2: Reproduced Git long-option abbreviations, `env -S`/`builtin eval`, npm/npx/Python server wrappers, task activation ambiguity, and a non-task rename-source exemption; exact follow-up reproduction passed after fixes.

Fixer:
- Added fail-closed handling and tests for every accepted reviewer case, required task activation/boundary changes to land separately, restricted task-card rename exemptions to `tasks/**/*.md`, and reduced commit-time checks to a measured smoke subset.

Quality Governor:
- Confirmed strict activation semantics, task-only rename exemptions, index-snapshot enforcement, fast/full check separation, and the 13-file TOOL boundary are aligned across implementation, tests, and documentation; approved the implementation for review.

## Verifier Evidence

- Command: `python3 scripts/agent/test_pre_bash_guard.py && python3 scripts/agent/test_check_staged_files.py && make check`
- Result: PASS on the exact staged implementation snapshot; independent verifier also ran the versioned pre-commit hook and `git diff --cached --check` successfully.
- Notes: Snapshot `0e3cec88ec4630d9c592b188792733dc0ee18338` contained 14 staged path records and no UI/package/static files. Full checks passed 107 deny + 43 allow guard cases, 4 formatter tests, 23 staged-file tests, and 11 validator tests; the allowlist suite includes real task-activation and scoped `git commit` transactions. Pre-commit passed its 12 deny + 5 allow smoke set in 1.91 seconds. This evidence-only task-card edit followed verification and does not change the verified implementation files.

## Failure Queue Items

- none
