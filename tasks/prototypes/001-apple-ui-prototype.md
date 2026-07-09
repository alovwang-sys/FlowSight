# Task: Preserve and Correct Apple UI Prototype

## Task Metadata

```yaml
task_id: PROTO-UI-001
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: [phase1, phase2, phase3, phase4, phase5]
depends_on: [TRIAL-001]
requires_gates: []
opens_gates: []
scope_override: preserve future-phase mocked UI interactions on a non-main prototype branch before phase0-sustained opens; this is not product acceptance
scope_override_approved_by: user request on 2026-07-10 to manage the existing frontend draft in the same Git repository
```

## Task ID

PROTO-UI-001

## Phase

Phase 0

## Goal

Version the Apple-style React draft in the FlowSight repository on a clearly isolated prototype branch, correct misleading data semantics, and prove its frontend/wheel build boundary without claiming any runtime phase is implemented.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Visual reference:
  - `/Users/amos/project_code/Flowsight/Apple 风格交互页面设计`
- This branch is a design and packaging prototype. It must not be merged as Phase 0 product evidence before `phase0-sustained` opens and downstream phase tasks accept the represented behavior.

## Related Fact IDs

- FS-003
- FS-015
- FS-016
- FS-017
- FS-022
- FS-026
- FS-028
- FS-031

## Allowed Files

- `package.json`
- `package-lock.json`
- `.node-version`
- `.prettierignore`
- `.prettierrc.json`
- `eslint.config.js`
- `tsconfig.json`
- `tsconfig.app.json`
- `tsconfig.node.json`
- `vite.config.ts`
- `ui/**`
- `flowsight/static/**`
- `tests/packaging/test_wheel_ui.py`
- `Makefile`
- `.github/workflows/agent-checks.yml`
- `tasks/prototypes/001-apple-ui-prototype.md`

The current task card and verifier evidence are always writable control-plane records.

## Expected Changed Files

- `package.json`
- `package-lock.json`
- `.node-version`
- `.prettierignore`
- `.prettierrc.json`
- `eslint.config.js`
- `tsconfig.json`
- `tsconfig.app.json`
- `tsconfig.node.json`
- `vite.config.ts`
- `ui/src/**`
- `flowsight/static/**`
- `tests/packaging/test_wheel_ui.py`
- `Makefile`
- `.github/workflows/agent-checks.yml`

## Forbidden

- Do not connect the prototype to a real sidecar or claim runtime collection works.
- Do not present logs, arbitrary tracepoint expressions, live intervention, or a static function call graph.
- Do not bind tracepoint configuration to a historical span or synthesize a hit/snapshot when configuration is created.
- Do not present this branch, its bundle, or its clean-wheel probe as an open Phase 0/1/2/3/4/5 gate.
- Do not merge this prototype branch to `main` before the required gates/tasks approve the represented behavior.

## Acceptance Criteria

- [ ] Root manifests lock Node/npm, TypeScript, React, React Flow, lint, test, and Prettier tooling; no formatter falls back to global PATH.
- [ ] `ui/` is the only frontend source and `flowsight/static/` is the deterministic, sourcemap-free Vite output with relative asset URLs.
- [ ] The code map labels and derives function-call edges from RuntimeSpan parent/child relationships; it makes no static function-call claim.
- [ ] Tracepoint configuration uses `codeNodeId + lineNo + locationHash`, remains project-level, and newly created configuration waits for a later request rather than fabricating a snapshot.
- [ ] Request snapshots remain separate from tracepoint configuration and stale reconfirmation supplies the current location hash.
- [ ] The UI is prominently marked as demo/prototype data and contains no logs, arbitrary expressions, or real-process pause semantics.
- [ ] `npm ci`, format/type/lint checks, unit tests, deterministic build, and the clean-wheel static bundle probe pass.
- [ ] Verification output explicitly states that this is prototype/packaging evidence, not a FlowSight runtime phase acceptance result.

## No-Test Reason

N/A

## Verification

Run:

```sh
npm ci
npm run check
npm test
npm run build:check
make check
```

Expected result:

```text
frontend checks and clean-wheel bundle probe pass; no runtime phase gate is claimed
```

## Risks

- A polished mock can be mistaken for implemented backend behavior.
- Configuration and request-scoped observation data can be incorrectly conflated.
- Generated assets can drift from source or be omitted from the Python wheel.

## Reviewer Focus

- Does any label or model still promise a static function call graph or fabricated tracepoint capture?
- Can the wheel probe accidentally import the workspace instead of the installed wheel?
- Are the prototype and phase-gate boundaries visible to both users and future agents?

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

- Command: `npm ci && npm run check && npm test && npm run build:check && make check`
- Result: TBD
- Notes: TBD

## Failure Queue Items

- none
