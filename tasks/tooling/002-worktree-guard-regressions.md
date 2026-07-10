# Task: Fix Worktree Detection and Direct Guard Regressions

## Task Metadata

```yaml
task_id: TOOL-002
release: v1
task_type: tooling
status: review
primary_phase: phase0
impacted_phases: []
depends_on: [TOOL-001, TRIAL-001]
requires_gates: []
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

TOOL-002

## Phase

Phase 0

## Goal

Make product-side detection ignore purely ignored frontend artifacts, and close the directly reviewable xargs/forced-push guard gaps without pretending the static guard is a general shell sandbox.

## Context

- Source of truth:
  - `AGENTS.md`
  - `docs/agent-operating-system.md`
  - `docs/agent-tooling.md`
  - `docs/agent-rule-authoring.md`
- Reproduced regression: an ignored `ui/node_modules` directory left by the prototype branch makes `main` fail `make check` even though it has no frontend source or manifests.
- Reproduced guard gaps: fixed `xargs` command execution and force-push ref rewrites are statically visible but currently allowed.

## Related Fact IDs

- FS-023
- FS-031

## Allowed Files

- `Makefile`
- `scripts/agent/pre_bash_guard.py`
- `scripts/agent/test_pre_bash_guard.py`
- `scripts/agent/test_product_detection.py`
- `docs/agent-operating-system.md`
- `docs/agent-tooling.md`
- `tasks/tooling/002-worktree-guard-regressions.md`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `Makefile`
- `scripts/agent/pre_bash_guard.py`
- `scripts/agent/test_pre_bash_guard.py`
- `scripts/agent/test_product_detection.py`
- `docs/agent-operating-system.md`
- `docs/agent-tooling.md`

## Forbidden

- Do not delete local dependency/build caches as a prerequisite for checks.
- Do not scan or attempt to interpret arbitrary Python/Perl/Ruby `-c` source strings.
- Do not claim the command guard is a security boundary or general sandbox.
- Do not change the current policy for `git reset --keep`, `--merge`, `--mixed`, or `--soft`.
- Do not modify FlowSight product or frontend source.
- Do not move full product tests or builds into pre-commit.

## Acceptance Criteria

- [x] `make check` passes on the Python-only `main` checkout when `ui/` contains only ignored artifacts such as `ui/node_modules`.
- [x] A non-ignored file under `ui/` still activates frontend scaffold validation and fails clearly when the root manifests are absent.
- [x] Product-side detection uses Git's tracked/non-ignored view rather than bare directory existence in check, fast-check, and product-test paths.
- [x] The guard rejects `xargs` as opaque stdin-driven command construction instead of claiming to parse its dynamic inputs.
- [x] The guard rejects force-push intent through `-f`, `--force`, abbreviations, force-with-lease, `+refspec`, and mirror forms while allowing ordinary push and `--no-force`.
- [x] Regression tests cover the new denials, safe controls, and ignored-only frontend checkout.
- [x] Documentation names interpreter/build-tool execution as a deliberate residual boundary and does not imply that CI can undo destructive commands.

## No-Test Reason

N/A

## Verification

Run:

```sh
python3 scripts/agent/test_product_detection.py
python3 scripts/agent/test_pre_bash_guard.py
make check
```

Expected result:

```text
ignored-only frontend artifacts do not activate frontend checks; direct guard regressions and all shared checks pass
```

## Risks

- A detector that ignores all untracked paths could hide a real partial frontend scaffold.
- Over-parsing xargs would recreate the same false-confidence problem as interpreter source scanning.
- Force-push matching must not reject `--no-force` or ordinary upstream setup.

## Reviewer Focus

- Does detection see tracked and non-ignored untracked UI source while excluding ignored caches?
- Can a direct Git ref rewrite still bypass normalization through wrappers or refspec syntax?
- Does the diff remain a small corrective slice rather than another general shell parser project?

## Role Outputs

Implementer:
- Replaced bare `ui/` existence checks with one tracked/non-ignored detector plus a non-Git fail-closed fallback; added five temporary-repository behavior tests; blocked direct/recognized-wrapper xargs and forced Git ref rewrites while documenting the interpreter boundary.

Adversarial Reviewer:
- Reviewer 1: Found the `-ofoo` push-option false positive and config-env/nonzero/signed mirror bypasses; after fixes, re-ran all reproductions plus safe controls and reported PASS with no remaining P0/P1.
- Reviewer 2: Found config-driven mirror/refspec bypasses, missing `-t` parsing, non-Git UI detection, and over-broad xargs wording; all accepted findings were fixed, exact pre-commit/index checks passed, and final review reported PASS.

Fixer:
- Added push-aware option/value parsing, direct and persistent `remote.*.mirror/push` protection including opaque `--config-env`, explicit safe controls, Git-aware/fail-closed product detection, and precise residual-boundary documentation without scanning interpreter source or changing reset policy.

Quality Governor:
- PASS: confirmed the seven-path Phase 0 tooling slice stays inside the allowlist and tool-neutral control plane, closes every reviewer finding with positive/negative tests, keeps the five integration tests out of `check-fast`, accurately documents residual interpreter/reset boundaries, and opens no product gate. No P0/P1/P2 remained.

## Verifier Evidence

- Command: `python3 scripts/agent/test_product_detection.py && python3 scripts/agent/test_pre_bash_guard.py && make check`
- Result: PASS
- Notes: Independent verifier froze Git-index tree `43aa71a5b1779ac5115570d30cde82e0c518f6ac` with exactly seven staged paths and reproduced it in an isolated repository. Product detection passed 5/5 in 2.70s with a physical ignored `ui/node_modules` fixture and zero Git-visible UI paths; guard passed 158 deny/80 allow in 14.06s; `make check` passed in 29.98s with formatter 4/4, allowlist 23/23, validator 11/11, Ruff, mypy, and Python 4/4. The real pre-commit passed in 6.48s with guard smoke 17/7. An unstaged fatal Makefile poison broke direct `make check-fast` but not the index-snapshot hook, proving worktree edits did not influence evidence. Output explicitly remained partial-scaffold evidence and did not open a Phase gate.

## Failure Queue Items

- none
