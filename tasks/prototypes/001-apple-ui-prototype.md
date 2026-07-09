# Task: Preserve and Correct Apple UI Prototype

## Task Metadata

```yaml
task_id: PROTO-UI-001
release: v1
task_type: implementation
status: complete
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

- [x] Root manifests lock Node/npm, TypeScript, React, React Flow, lint, test, and Prettier tooling; no formatter falls back to global PATH.
- [x] `ui/` is the only frontend source and `flowsight/static/` is the deterministic, sourcemap-free Vite output with relative asset URLs.
- [x] The code map labels and derives function-call edges from RuntimeSpan parent/child relationships; it makes no static function-call claim.
- [x] Tracepoint configuration uses `codeNodeId + lineNo + locationHash`, remains project-level, and newly created configuration waits for a later request rather than fabricating a snapshot.
- [x] Request snapshots remain separate from tracepoint configuration and stale reconfirmation supplies the current location hash.
- [x] The UI is prominently marked as demo/prototype data and contains no logs, arbitrary expressions, or real-process pause semantics.
- [x] `npm ci`, format/type/lint checks, unit tests, deterministic build, and the clean-wheel static bundle probe pass.
- [x] Verification output explicitly states that this is prototype/packaging evidence, not a FlowSight runtime phase acceptance result.

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
- Moved the Apple-style React draft into the root monorepo contract, added exact frontend toolchain locks and CI wiring, generated a deterministic wheel-owned static bundle, and corrected the prototype's runtime/tracepoint/source-identity semantics without connecting a sidecar.

Adversarial Reviewer:
- Reviewer 1: Found conflated runtime/static edges, historical/current source identity, request/config state, repeated-call selection, and single-Tracepoint assumptions; all accepted P0/P1 findings were fixed, and the final 7-test follow-up found no residual blocker.
- Reviewer 2: Independently reproduced current-hash, multi-Tracepoint, snapshot-identity, and repeated-node-active risks; all P1 findings closed, and the remaining P2 direct current-hash assertion was added before verification.

Fixer:
- Derived map edges only from RuntimeSpan parent/child IDs; separated CodeNode, RuntimeSpan, Tracepoint, and Snapshot identities; supported up to five independent named-line Tracepoints; fixed repeated invocation activity; added a distinct HTTP entry node; and hardened clean-wheel/static determinism tests.

Quality Governor:
- PASS: approved the 32-path index slice on `codex/apple-ui-prototype`; scope override, `opens_gates: []`, task allowlist, tool-neutral checks, mock-only data source, deterministic bundle, loopback wheel probe, and prototype disclosure all align with the operating rules. No P0/P1 remained; local Node/npm warning-only enforcement is a non-blocking P2 covered by exact-version CI.

## Verifier Evidence

- Command: exact `git checkout-index` snapshot followed by `npm ci`, `npm run check`, `npm test`, `npm run build:check`, and `make check`
- Result: PASS
- Notes: Independent verifier froze staged tree `201ba07226fd30ebfdeaa13482359953e714eae2` with 32 implementation paths. npm installed 257 packages with 0 vulnerabilities; Prettier, both TypeScript configs, ESLint, Vitest 7/7, deterministic Vite output, guard 107/43, formatter 4/4, allowlist 23/23, validator 11/11, Ruff, mypy, Python 4/4, and clean-wheel UI probe 1/1 passed. The wheel probe imported from an isolated site-packages with empty `PYTHONPATH`, no Node, complete relative assets, no sourcemaps, and `127.0.0.1:0` only. Local Node 26.4.0/npm 11.17.0 produced the expected engine warning against pinned 22.23.1/10.9.8. GitHub Actions run 29056593134 then passed all four Ubuntu/macOS × CPython 3.12/3.13 jobs for commit `5ed48a2988307bddbfd502fe1cfecb60cdfe634e`, including exact Node/npm selection. Output explicitly states prototype/packaging evidence opens no runtime phase gate.

## Failure Queue Items

- none
