# Task: Spike Tracepoint Backend Go/No-Go

## Task Metadata

```yaml
task_id: TRIAL-005
release: v1
task_type: spike
status: in_progress
primary_phase: phase4
impacted_phases: []
depends_on: [TRIAL-003]
requires_gates: []
opens_gates: [phase4-tracepoint]
spike_decision: pending
scope_reductions: sys.monitoring only; exact non-generator sync/coroutine functions and methods; explicitly context-propagated thread-pool work; standard GIL CPython 3.12/3.13 with safe tool ID and frame self-probe; real debugpy/coverage unsupported
scope_override: v1 tracepoints are limited to the five approved TRIAL-005 reductions recorded in spikes/tracepoint_backend/RESULT.md
scope_override_approved_by: user on 2026-07-11
```

## Task ID

TRIAL-005

## Phase

Phase 4

## Goal

Determine which tracepoint backend and function shapes can satisfy before-line, named-variable-only capture without global tracing, async cross-request leakage, or interference with existing tracers.

## Context

- Source of truth:
  - `docs/flowsight-mvp-design.md`
  - `AGENTS.md`
  - `docs/agent-facts.tsv`
- Related design sections:
  - 5.5 Tracepoint
  - 6.4 Safe summaries
  - 6.5 Tracepoint backend risk gate

## Related Fact IDs

- FS-015
- FS-016
- FS-017
- FS-020
- FS-033

## Allowed Files

- `spikes/tracepoint_backend/**`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`
- `docs/agent-facts.tsv`
- `pyproject.toml`
- `Makefile`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `spikes/tracepoint_backend/`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`
- `docs/agent-facts.tsv`

## Forbidden

- Do not implement the production tracepoint UI/API.
- Do not enable interpreter-wide global tracing.
- Do not evaluate user expressions or capture unspecified locals.
- Do not describe a presumed fallback as supported without evidence.

## Acceptance Criteria

- [x] Standard GIL-enabled CPython 3.12/3.13 sync, async/await, thread-pool, nested-call, concurrent-request, exception, and cancellation cases are exercised; free-threaded builds remain unsupported.
- [x] Tests prove before-line semantics, named-vars-only capture, and no capture from unrelated functions or requests.
- [x] Existing debugger/coverage tracer, enable/disable, cleanup, and monitoring tool-ID conflicts are tested or explicitly rejected with evidence.
- [x] `sys.monitoring` frame-locals feasibility and any scoped `sys.settrace` isolation are measured with a frozen overhead harness.
- [ ] Result is exactly a supported backend, a documented narrowed subset, or Phase 4 no-go; the design/facts are updated accordingly.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/spikes/test_tracepoint_backend.py
make check
```

Expected result:

```text
all backend spike tests pass and a reviewed support-matrix decision is recorded
```

## Risks

- Thread-level tracing may leak across async tasks even when callbacks filter frames.
- A monitoring callback may discover the wrong frame or depend on unstable interpreter behavior.
- Benchmarks may hide debugger/coverage interference.

## Reviewer Focus

- Does a tracer survive `await`, cancellation, exception, or shutdown?
- Are unsupported combinations rejected at configuration time?
- Is the conclusion narrower than the evidence rather than broader?

## Role Outputs

Implementer:
- Primary Codex agent: implemented the smallest isolated Phase 4 backend slice,
  negative fallback probe, frozen benchmark, and colocated regressions without
  production UI/API or global tracing.

Adversarial Reviewer:
- Reviewer 1: backend/lifecycle review found constructor bypass, pre-admission
  callback drain, foreign monitoring-state mutation, concurrent lifecycle
  timeout, and free-threaded build/runtime defects; the final frozen-tree
  review reported no remaining P0/P1/P2.
- Reviewer 2: evidence review found incomplete digest coverage, missing numeric
  budget, overbroad function/tracer claims, non-function code acceptance, and
  definition-line ambiguity; the final evidence review reported no remaining
  P0/P1/P2.

Fixer:
- Primary Codex agent: applied all accepted findings with deterministic
  regressions, narrowed claims to tested mechanisms, and froze six executable
  performance guards.

Quality Governor:
- Independent review confirmed Phase 4 discipline, allowlist compliance,
  candidate/final separation, and correct closed-gate behavior. It requires
  explicit scope approval and immutable matrix evidence before finalization.

## Verifier Evidence

- Commit: `9f195d33ba77f11bde103b91ba94fe5872c73591`
- Commands: `.venv/bin/python -m pytest tests/spikes/test_tracepoint_backend.py`;
  `/tmp/flowsight-trial004-py312/bin/python -m pytest tests/spikes/test_tracepoint_backend.py`;
  `make check`; `make gate-phase4`
- Result: CPython 3.13.5 and 3.12.11 each passed 31 focused tests and produced
  digest `sha256:3993fd75a45b1e14be3e04d56534928cadc928a92dce5af6473398e5c14c30e9`;
  immutable `make check` passed all agent/static checks and 217 tests.
- Notes: `make gate-phase4` reran 217 tests successfully, then remained closed
  only on pending TRIAL-005 decision/status and planned FS-015/016/017/020/033
  evidence. The local candidate is `go-with-scope-reductions`; final decision is
  not recorded before explicit approval and the immutable four-job CI matrix.

## Failure Queue Items

- `TRIAL-005-CI`: push the immutable candidate only after explicit authorization,
  pass macOS/Linux × CPython 3.12/3.13, and record the run URL/ID.
- `TRIAL-005-FACT-PROMOTION`: after approval and CI, promote the five Phase 4
  gate facts, record the final decision, complete the card, and rerun the gate.
