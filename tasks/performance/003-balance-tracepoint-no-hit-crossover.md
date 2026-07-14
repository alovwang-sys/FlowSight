# Task: Balance Tracepoint No-Hit Crossover Statistic

## Task Metadata

```yaml
task_id: PERF-003
release: v1
task_type: spike
status: complete
primary_phase: phase4
impacted_phases: []
depends_on: [PERF-002]
requires_gates: [phase4-tracepoint]
opens_gates: []
spike_decision: go
scope_reductions: none
scope_override: none
scope_override_approved_by: none
```

## Task ID

PERF-003

## Phase

Phase 4

## Goal

Replace each no-hit two-leg pair with a symmetric four-leg crossover block
whose ratio cancels multiplicative within-pair drift while still failing a real
active-side CPU regression.

## Context

- FSQ-0001 recurred on the unchanged schema-v3 harness in PR run
  `29297091756`: Ubuntu CPython 3.13 observed a median paired thread-CPU ratio
  of `1.2023160302028435` against the unchanged `1.15` maximum.
- The same SHA passed all four push jobs in run `29297089621`, and the PR run's
  two macOS jobs passed. Ubuntu CPython 3.12 completed all 2766 tests before
  matrix fail-fast cancelled it after the 3.13 failure.
- The existing 21-pair AB/BA crossover records 11 AB and 10 BA samples. Taking
  the median of all raw ratios can select the majority order when the two order
  cohorts separate under multiplicative CPU-frequency drift.
- Schema v4 will measure 21 same-seed ABBA/BAAB blocks. Each block computes
  `(active_1 + active_2) / (baseline_1 + baseline_2)`, so reciprocal order drift
  cancels inside each sample while a common active-side factor remains.

## Related Fact IDs

- FS-015
- FS-016
- FS-017
- FS-020
- FS-033

## Allowed Files

- `spikes/tracepoint_backend/benchmark.py`
- `spikes/tracepoint_backend/RESULT.md`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`
- `queue/failures.jsonl`
- `tasks/performance/001-tracepoint-benchmark-scheduler-noise.md`
- `tasks/performance/002-reconcile-tracepoint-performance-evidence.md`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `spikes/tracepoint_backend/benchmark.py`
- `spikes/tracepoint_backend/RESULT.md`
- `tests/spikes/test_tracepoint_backend.py`
- `docs/flowsight-mvp-design.md`
- `queue/failures.jsonl`
- `tasks/performance/001-tracepoint-benchmark-scheduler-noise.md`
- `tasks/performance/002-reconcile-tracepoint-performance-evidence.md`

## Forbidden

- Do not raise, remove, skip, or retry-until-green any accepted threshold.
- Do not change the backend, safe-summary implementation, supported runtime
  matrix, product code, CI matrix, public API, frontend, or Phase 1 telemetry.
- Do not hide raw ratios or the existing nearest-rank p95 check.
- Do not claim verification from a rerun of unchanged code.

## Acceptance Criteria

- [x] Schema v4 reports 21 ABBA/BAAB no-hit blocks, their four raw legs, and the
      block ratios used by the existing median and nearest-rank p95 checks.
- [x] A deterministic reciprocal-drift test proves the old raw median can fail
      while the order-balanced estimator passes at no real overhead.
- [x] A deterministic test proves a real common active-side factor above `1.15`
      still fails after order balancing.
- [x] All six numeric maxima remain unchanged and the new digest/evidence are
      consistent in the benchmark, RESULT, and MVP design.
- [x] FSQ-0001 is marked fixed only after the patch and verified only after the
      focused, repository, Phase 4, push, and PR gates pass.
- [x] CPython 3.12/3.13 focused tests, `make check`, and `make gate-phase4` pass.

## No-Test Reason

N/A

## Verification

Run:

```sh
python -m pytest tests/spikes/test_tracepoint_backend.py
make test-trial005
make check
make gate-phase4
git diff --check
```

Expected result:

```text
schema-v4 order balancing rejects real CPU regressions without majority-order false failures
```

## Risks

- An incorrectly oriented BA ratio could hide a true active-side regression.
- Dropping raw or p95 evidence could trade one false failure for a false green.
- Digest or historical-evidence drift could make schema-v3 results appear to
  validate schema v4.

## Reviewer Focus

- Does each symmetric four-leg block cancel only reciprocal AB/BA order drift?
- Does a common active/baseline factor survive the block ratio and fail at the
  unchanged maximum?
- Are schema-v3 evidence and failures clearly historical after schema v4?
- Is the diff confined to Phase 4 benchmark evidence with no product behavior?

## Role Outputs

Implementer:
- Added same-seed alternating ABBA/BAAB no-hit blocks, preserved raw leg
  evidence, advanced the digest to schema v4, and synchronized design/result
  evidence without changing backend or product code.

Adversarial Reviewer:
- Reviewer 1: Found one P1 because XOR could accept different cross-pair
  checksums, plus three P2 evidence/status issues. Four-checksum equality,
  mismatch/orchestration tests, calibration wording, queue evidence, and
  pre-CI decision state were corrected. A second P1 correctly rejected stale
  pre-fix verification; the entire fixed-digest sequence was rerun. Final
  re-review found no P0/P1/P2.
- Reviewer 2: waived unless the first review finds a distinct statistics or
  false-green risk.

Fixer:
- Reopened FSQ-0001 after the repeated signature and replaced the
  majority-order sample construction without changing any numeric maximum.

Quality Governor:
- The diff is Phase 4 benchmark/test/evidence only. It does not change the
  supported runtime matrix, Phase 1 telemetry, public API, frontend, backend,
  safe-summary behavior, product code, or CI matrix.

## Verifier Evidence

- Command: `.venv/bin/python -m pytest tests/spikes/test_tracepoint_backend.py`;
  `/tmp/flowsight-trial004-py312/bin/python -m pytest tests/spikes/test_tracepoint_backend.py`;
  `make test-trial005`; `make check`; `make gate-phase4`;
  `git diff --check`; `python3 scripts/validate_agent_system.py`; digest/component
  comparison; candidate push/PR CI inspection.
- Result: passed
- Notes: CPython 3.13.5 and 3.12.11 passed 41 focused tests in 14.05 and 15.51
  seconds. `make test-trial005` passed 41 tests in 14.66 seconds; `make check`
  passed 2771 tests in 120.22 seconds; `make gate-phase4` passed 2771 tests in
  119.10 seconds plus the Phase 4 gate.
  Digest is
  `sha256:3bcbcc7d7f6ae1b14ef0672994e00f45ad42b2c73827e451a98fd4414380ec9d`;
  backend and safe-summary component digests are unchanged. `git diff --check`
  and agent-system validation passed. Candidate
  `587964c958a44749629e60ab64b77689d4764540` passed all eight jobs across push
  run `29298619075` and PR run `29298620645`; FSQ-0001 is verified.

## Failure Queue Items

- FSQ-0001
