# Task: Harden Agent Enforcement Boundaries

## Task Metadata

```yaml
task_id: TOOL-001
release: v1
task_type: tooling
status: in_progress
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
- `scripts/agent/check_staged_files.py`
- `scripts/agent/test_check_staged_files.py`
- `scripts/agent/post_edit_format.py`
- `scripts/agent/test_post_edit_format.py`
- `.githooks/pre-commit`
- `AGENTS.md`
- `Makefile`
- `docs/agent-tooling.md`

## Forbidden

- Do not claim that local hooks are impossible to bypass.
- Do not make pre-commit run the clean-wheel or full frontend build.
- Do not weaken CI `make check`.
- Do not modify FlowSight product source or frontend source.

## Acceptance Criteria

- [ ] Guard rejects shell wrappers, absolute Git paths, multiline destructive commands, `commit --no-verify`, destructive checkout/restore paths, public-bind environment prefixes, and broad repository/parent `rm -rf` targets.
- [ ] Guard tests include safe branch switching, safe loopback binding, and narrow temporary-directory deletion to control false positives.
- [ ] A staged-file checker rejects zero/multiple active tasks where product changes are staged and rejects staged paths outside the single active task's `Allowed Files`.
- [ ] Rename/delete/control-plane/current-task-card behavior is covered by tests in temporary Git repositories.
- [ ] Pre-commit runs a fast deterministic subset; CI and explicit `make check` retain full Python/frontend/wheel verification.
- [ ] Python formatting uses the project interpreter and pinned Ruff module; frontend formatting uses the repository's locked Prettier binary rather than arbitrary global PATH tools.
- [ ] Documentation states that hooks remain bypassable by humans and that protected-branch CI is the merge guarantee.

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
- TBD

Adversarial Reviewer:
- Reviewer 1: TBD
- Reviewer 2: TBD

Fixer:
- TBD

Quality Governor:
- TBD

## Verifier Evidence

- Command: `python3 scripts/agent/test_pre_bash_guard.py && python3 scripts/agent/test_check_staged_files.py && make check`
- Result: TBD
- Notes: TBD

## Failure Queue Items

- none
