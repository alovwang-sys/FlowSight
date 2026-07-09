# FlowSight Agent Rules

FlowSight is a local runtime visualization map for Python FastAPI developers and coding agents. Agents must treat `docs/flowsight-mvp-design.md` as the product source of truth and this file as the engineering operating manual.

## Read First

Before changing code, read:

1. `docs/flowsight-mvp-design.md`
2. `docs/agent-operating-system.md`
3. `docs/agent-facts.tsv`
4. The task card for the current task, if one exists

If these files conflict, use this priority order: explicit user request, `AGENTS.md`, task card, MVP design doc, local code conventions.

If a user request intentionally crosses a v1 non-goal, the task must name it as a scope override before implementation starts.

## v1 Scope

v1 is only:

- Standard GIL-enabled CPython 3.12/3.13 FastAPI local development and test environments.
- A Python SDK in the business process plus one project-scoped local Python sidecar that owns UI/API/SQLite.
- Single-worker FastAPI/Uvicorn, including normal startup and reload reconnection.
- Backend HTTP request traces.
- Replay after the request finishes, not live intervention.
- OTel HTTP/DB/API spans plus explicitly decorated `@flowsight.trace` function spans and source inspector.
- SQLite local storage.
- Tracepoints that capture specified local variable names only.
- A bundled TypeScript/React Flow code map plus timeline and inspector UI; end users do not need Node.js.

## v1 Non-Goals

Do not implement these in v1 tasks:

- Multi-language support.
- Cross-service distributed tracing.
- Production remote debugging.
- Runtime pause/resume of the real application.
- Arbitrary Python expression breakpoints.
- Full variable tracing.
- WebSocket live streaming.
- VS Code extension.
- AI automatic diagnosis.
- Long-term trace storage.
- Flame graph or CPU profiler.
- Celery, background task, or complex async tracing.
- ValueRef/ValueEdge data lineage tables.
- Multi-worker aggregation or process-manager compatibility.
- Log collection/query; v1 shows safe exceptions and span events only.
- Function-level static call-graph claims.
- Automatic `include_packages` function wrapping; v1 uses explicit `@flowsight.trace` only.
- Windows sidecar/process lifecycle until a dedicated compatibility task adds evidence.
- Free-threaded CPython builds until trace/lifecycle isolation has dedicated evidence.

Data lineage is v1.1 experimental. v1 may store args/return summaries on spans, but it must not build a lineage engine.

## Phase Discipline

Every implementation task must name exactly one primary phase from the MVP design. Cross-cutting work may list impacted phases, release, task type, and dependencies, but its observable behavior must remain bounded by the primary phase:

- Phase 0: installable skeleton, sidecar process model, pre-persistence safety boundary, SQLite WAL writer, bundled empty UI.
- Phase 1: OTel ingest and waterfall.
- Phase 2: static code map.
- Phase 3: function-level path and source inspector.
- Phase 4: restricted tracepoint and variable snapshots.
- Phase 5: replay, linking, polish.
- Phase 6: v1.1 experimental data lineage, not v1 acceptance.

If a task crosses phases, split it before coding.

Pre-implementation spikes are still assigned one primary phase and must produce a go/no-go decision rather than shipping hidden later-phase behavior.

## Agent Workflow

Use this loop for every non-trivial change:

1. Identify the phase and smallest verifiable slice.
2. State the intended files and validation command.
3. Add or update tests with the implementation.
4. Keep changes inside the task's allowed files.
5. Run the narrowest relevant verification.
6. Review the diff as if it were wrong.
7. Summarize what changed and what evidence passed.

Do not make broad refactors, dependency changes, formatting sweeps, or generated-file churn unless the task explicitly requires them.

## Verification Contract

The repository must grow toward these stable commands:

- `make check`: format, lint, type check, and tests.
- `make test`: full automated test suite.
- `make test-phase0`, `make test-phase1`, etc.: phase-specific checks when useful.
- `make demo-fastapi`: starts or verifies the demo integration.

If a command does not exist yet, the task that needs it should add the smallest useful version.

Until product source/tests exist, a passing bootstrap `make check` is only agent-system evidence and must print that limitation. It must never be reported as Phase 0 or product acceptance. Once the package scaffold exists, `make check` must actually run format-check, lint, type checks, Python tests, and any applicable frontend checks.

Shared agent guard logic belongs in `scripts/agent/`. Tool-specific directories such as `.claude/` must only adapt that shared logic for a specific agent client.

## Testing Rules

- Every behavioral change ships with an automated test in the same change.
- Prefer targeted tests near the changed code.
- Avoid sleeps; wait for observable conditions with bounded timeouts.
- Use temporary directories for filesystem tests.
- Bind local servers to `127.0.0.1`.
- Use port `0` for incidental test servers unless testing explicit port selection.
- Tests for redaction must include password, token, secret, cookie, session, and API key shapes.
- Tests for tracepoints must prove only named variables are captured.
- Tests for tracepoint line semantics must assert "before this line executes".
- Process tests must cover ordinary startup, one real Uvicorn reload, duplicate initialization, stale state/lock recovery, explicit shutdown, and unsupported multi-worker registration.
- OTel tests must cover an existing `TracerProvider`/exporter, idempotent instrumentation, no duplicate root span, private exception enrichment, bounded flush, and trace complete/incomplete states.
- All persisted event classes must have pre-queue redaction tests for query/header/SQL/exception/args/return/span-event/snapshot shapes.
- The clean-wheel UI-serving probe must be self-contained and run from a temporary directory with empty `PYTHONPATH`; it may not rely on workspace `conftest.py` or source imports, and must prove the installed wheel serves the bundled UI without Node.js.

## Runtime Safety Rules

- Never enable global tracing across the whole interpreter.
- Never evaluate user-provided Python expressions for tracepoints.
- Never store raw Python objects in SQLite.
- Never call unknown object methods while serializing snapshots.
- Never call unknown `__repr__`, properties, iterators, or custom serializers while creating any persisted summary.
- Never expose UI/API beyond `127.0.0.1` in v1.
- Never treat loopback binding as authentication; require a startup capability token plus Host/Origin checks.
- Never silently ignore storage, ingest, or flush failures.
- Every SDK/sidecar queue must be bounded and expose dropped/error counts.
- Every background thread, queue, or sidecar process must have an idempotent shutdown/flush path.
- Only the sidecar may own the UI/API listener or write SQLite.

## Data and Privacy Rules

- Every persisted runtime value must be sanitized before it enters the SDK queue or crosses the process boundary; serialization must limit depth, size, element count, and total payload.
- Redaction is default-on, not optional.
- Values whose name or path includes password, passwd, pwd, token, secret, key, auth, credential, cookie, session, api_key, access_token, or refresh_token must be redacted.
- Exceptions, span events, args/return summaries, and snapshots must store safe summaries, not raw text or live object references.

## Git and Workspace Rules

- Do not run `git reset --hard`, `git clean`, `git checkout --`, `git restore --source`, or `git stash` unless the user explicitly asks.
- Do not revert user changes.
- Do not edit unrelated files.
- Do not delete or weaken tests to make a task pass.

## Review Roles

For larger tasks, split review into independent roles:

- Implementer: writes the smallest slice.
- Adversarial Reviewer: assumes the diff is wrong and looks for bugs, scope creep, missing tests, and boundary violations.
- Fixer: applies only accepted review findings.
- Verifier: runs checks and summarizes evidence.
- Quality Governor: checks that the task system itself still matches the FlowSight agent operating rules.

## Before Normal Implementation

Before sustained Phase 0 implementation, run Trial 1, Trial 2, and the Phase 0 process/OTel Trial 4 from `docs/agent-trial-plan.md`. Run the safe-summary Trial 3 before any Phase 1 telemetry persistence. Run the tracepoint backend Trial 5 before Phase 4. Trial 4 and Trial 5 must record explicit go/no-go decisions and scope reductions. Trial results should update task templates, facts, hooks, or review rules before the project scales.

## Tool Parity (Claude Code and Codex)

FlowSight must behave the same whichever agent is the main developer. The goal is
equivalent guarantees, not identical agent behavior. Enforcement therefore lives
on neutral choke points, not inside any one tool:

- Shared rules live in `AGENTS.md`, `docs/`, `docs/workflows/`, `tasks/`,
  `scripts/agent/`, and the `Makefile`.
- The repository-merge guarantee is `make check`, run on every commit by
  `.githooks/pre-commit` (enable once per clone with `make init`) and on every
  push/PR by `.github/workflows/agent-checks.yml`. It validates committed-state invariants; it cannot retroactively prevent or repair destructive commands against uncommitted files.
- Tool adapters are thin: `.claude/` for Claude Code, `codex.config.example.toml`
  for Codex. They must only call the shared logic, never contain rules of their
  own.
- Role SOPs are canonical in `docs/workflows/*.md`; `.claude/commands/*` are
  name-for-name pointers to them.

Before adding any new rule or guard, read `docs/agent-rule-authoring.md` and place
it in a tool-neutral location first. See `docs/agent-tooling.md` for the full
per-tool wiring map. Do not add enforcement that only one tool runs.
