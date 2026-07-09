# FlowSight Agent Operating System

This document turns the Bun-style agent workflow into a FlowSight-specific engineering system. The goal is to let coding agents move quickly because the allowed surface is narrow, observable, and reviewable.

## What We Borrow From Bun

Bun's useful pattern is not "let AI write a lot of code." It is:

- Put all agents behind one project rulebook.
- Convert implicit reviewer knowledge into files.
- Make build, test, lint, and CI failures the task queue.
- Use independent implementer, reviewer, fixer, and verifier roles.
- Block dangerous commands with hooks.
- Format edited files automatically where safe.
- Start with mechanical, verifiable slices before redesign.

For FlowSight, the equivalent is: agents do not decide product direction. They fill one phase-labeled, testable gap at a time.

## Files That Govern Agents

Use these files as the project-level control plane:

- `AGENTS.md`: mandatory rules and v1 boundaries.
- `docs/flowsight-mvp-design.md`: product and architecture source of truth.
- `docs/agent-operating-system.md`: workflow, roles, and quality gates.
- `docs/agent-task-template.md`: task card format.
- `docs/agent-facts.tsv`: machine-readable FlowSight invariants.
- `docs/agent-failure-queue.md`: structured failure backlog format.
- `docs/agent-trial-plan.md`: pre-implementation trial tasks.
- `scripts/agent/pre_bash_guard.py`: canonical command guard logic.
- `scripts/agent/post_edit_format.py`: canonical post-edit formatter logic.
- `scripts/agent/test_pre_bash_guard.py`: command guard smoke tests.
- `scripts/agent/test_validate_agent_system.py`: task metadata and gate smoke tests.
- `.claude/settings.json`: optional Claude Code hook wiring.
- `.claude/hooks/*`: Claude Code adapters that call `scripts/agent/*`.
- `.claude/commands/*`: optional fixed workflows for implementation, adversarial review, and verification.

As the project matures, add focused rule files instead of expanding prompts. Add these only when a module exists, a rule is repeatedly needed, or a review failure shows the missing contract is load-bearing:

- `docs/testing.md`
- `docs/security-and-redaction.md`
- `docs/storage-contract.md`
- `docs/tracepoint-contract.md`
- `docs/ui-contract.md`
- `docs/api-contract.md`

## Work Intake

Every coding task should begin as a task card. A good task card contains:

- Release, task type, status, and exactly one primary phase.
- Impacted phases, dependencies, prerequisite `requires_gates`, and `opens_gates` membership.
- Goal.
- Allowed files.
- Forbidden files or behavior.
- Acceptance criteria.
- Verification commands.
- Known risks.
- Explicit scope override and approval when a v1 non-goal is intentionally crossed.

The current task card itself and its verifier evidence are control-plane records. They may be updated without being part of the product-code allowlist; all other code and docs remain constrained by `Allowed Files`.

Task status normally moves `planned -> in_progress -> review -> complete`; use `blocked` only with a recorded blocker. A task cannot enter `in_progress`, `review`, or `complete` while a dependency is incomplete. `complete` requires checked acceptance criteria, filled role outputs, passing verifier evidence, and no `TBD`. Gates are evaluated separately from ordinary `make check` so planned future work does not create vacuous green phase evidence.

`requires_gates` contains gates that must already be open before a downstream task starts. `opens_gates` is membership in a fixed, version-controlled gate opener set. Direct task prerequisites belong in `depends_on`.

Tasks should be small enough that one failing test, one missing API route, one schema migration, or one UI interaction can define completion.

Good task shapes:

- "Implement `TraceRepository.insert_trace` and tests."
- "Expose `GET /api/traces` using fake seeded data."
- "Mark tracepoints stale when `location_hash` changes."
- "Add redaction tests for token-like strings."

Bad task shapes:

- "Build the whole runtime collector."
- "Make the UI nice."
- "Add tracepoints."
- "Refactor storage."

## Role Model

### Implementer

The implementer owns one task card and only that task card.

Required behavior:

- Read the relevant docs.
- Keep changes inside allowed files.
- Add tests with behavior.
- Avoid phase creep.
- Report verification results.

### Adversarial Reviewer

The reviewer must not share the implementer's justification. It reviews the diff as suspicious.

Review checklist:

- Does this break a v1 boundary?
- Did it add behavior outside the task?
- Are tests capable of failing for the right reason?
- Are privacy, redaction, and storage constraints preserved?
- Are background threads, queues, and server ports safe?
- Did it invent a new abstraction instead of using the planned module?

### Fixer

The fixer applies only review findings that are concrete and evidenced.

Required behavior:

- Do not broaden the task while fixing.
- Do not silently delete tests or comments.
- Explain any rejected reviewer suggestion with evidence.

### Verifier

The verifier runs commands and summarizes evidence.

Required behavior:

- Run the narrowest relevant command first.
- Run `make check` before a major merge once it exists.
- Report commands exactly.
- Report skipped verification honestly.

### Quality Governor

The quality governor checks the agent system, not just the code.

Checklist:

- Does the task map to one phase?
- Is the task small enough for independent review?
- Are hooks and docs still aligned with actual commands?
- Did the implementation reveal a new rule that belongs in docs?
- Did agents follow the allowed/forbidden file boundaries?

## Queue Design

FlowSight should treat machine failures as backlog:

- Type errors become implementation tasks.
- Failing tests become bug tasks.
- Lint failures become cleanup tasks.
- CI failures become verifier-owned tasks.
- Redaction or tracepoint boundary violations become safety tasks.

Each queue item should include:

- Failing command.
- Minimal failure excerpt.
- Suspected owner module.
- Acceptance condition.
- Link to the task card or issue.

## Phase Gates

A gate has three independent conditions: every task in the version-controlled opener set is `complete`; every spike opener decided `go` or `go-with-scope-reductions` (a completed `no-go` keeps the capability gate closed); and every fact in the validator's canonical gate-fact set has `command:` verification plus one or more existing test paths rather than `planned:`/`manual:` evidence. Gate targets run `make check` before evaluating those records. Task cards declare opener membership with `opens_gates`; downstream tasks declare prerequisites with `requires_gates`. The same gate-state function protects both `--gate` and active downstream tasks, so editing a card cannot silently add/remove an opener or bypass a closed gate.

### Pre-Implementation Risk Gates

Required before sustained Phase 0/1 work:

- Trial 1 proves the SDK public shape without collector scope creep.
- Trial 2 proves bounded single-writer SQLite behavior, concurrent producers, graceful flush, and visible failures.
- Trial 4 produces a go decision for sidecar singleton ownership, reload reconnection, OTel coexistence, and bounded lifecycle.
- Trial 3 proves the safe-summary primitive before any Phase 1 telemetry persistence.

Required before Phase 4:

- Trial 5 produces a backend/function-shape support matrix for tracepoints. A no-go narrows Phase 4; it must not be hidden behind a presumed fallback.

### Phase 0 Gate

Required:

- Python package exists.
- Demo FastAPI app exists.
- `FlowSight.init_app(app)` starts or attaches to exactly one project-scoped sidecar.
- UI/API binds only `127.0.0.1`.
- UI/internal/write APIs require a startup token and validate Host/Origin as applicable.
- SQLite uses WAL.
- Only sidecar writes SQLite; SDK and writer queues are bounded and expose drops/errors.
- Safe conversion runs before SDK enqueue for every persisted event class.
- Normal startup, one real reload, duplicate init, unsupported multi-worker, and bounded shutdown flush are tested.
- The built wheel serves the bundled UI without a Node runtime.

Forbidden:

- OTel complexity before process model works.
- Remote binding.
- Real tracepoint implementation.
- Full OTLP receiver compatibility.

### Phase 1 Gate

Required:

- OTel FastAPI server spans are the only source of OTel trace/span identity; FlowSight derives a separate `request_trace_id` per local server request and never creates a duplicate root.
- One `FlowSightSpanProcessor` enqueues safe versioned internal events for the sidecar without replacing a user's provider/exporters.
- Idempotent and out-of-order ingest writes complete/incomplete traces and spans to SQLite.
- UI shows trace list and basic waterfall.
- Query API is covered by tests.
- A finished request is finalized in the UI within one second.
- Sensitive request/OTel/SQL/exception fixtures never enter the SDK queue or SQLite as raw values.

Forbidden:

- Hand-rolled tracing where OTel already covers it.
- Any stable function-level auto-wrapper; v1 maps the OTel server span and uses explicit decorators.
- Logging collection or raw OTLP persistence.

### Phase 2 Gate

Required:

- LibCST extracts file/module/class/function CodeNode records.
- `stable_key` does not include line numbers.
- `location_hash` detects source drift.
- Route-to-handler mapping exists.
- RuntimeSpan records `source_hash_at_capture`; source reads stay inside project root.

Forbidden:

- Claiming static call graph is complete.
- Function-level static `calls` edges in stable v1.

### Phase 3 Gate

Required:

- `@flowsight.trace` creates function spans.
- OTel server spans link to route handler CodeNode when possible.
- Inspector can show safe args/return summaries.
- Undecorated business functions are shown as unobserved, not implied to be traced.

Forbidden:

- Any `include_packages` package auto-wrapping in v1.

### Phase 4 Gate

Required:

- Tracepoint line must be executable.
- Trial 5 has a recorded go decision and support matrix.
- Tracepoint captures only named local variables.
- Semantics are "before line executes."
- Serializer is redacted and bounded.
- Drifted CodeNode makes tracepoint stale.

Forbidden:

- Arbitrary expressions.
- Global tracing.
- Raw object storage.

### Phase 5 Gate

Required:

- Timeline replay controls are UI-only.
- Graph, timeline, and inspector stay linked.
- Slow spans are visually obvious.

Forbidden:

- Runtime pause/resume.
- WebSocket live mode.

Phase 5 also requires clean-install, browser E2E, real reload, bounded-overload/storage-failure tests, a 100-request resource check, and the baseline overhead budget from the MVP design.

## Hook Policy

Hooks are guardrails, not a replacement for review.

The canonical hook logic lives under `scripts/agent/`. Tool-specific directories such as `.claude/` should only adapt their tool protocol to the canonical scripts.

Pre-command guards should deny:

- Destructive git commands.
- `rm -rf` forms that can wipe the workspace.
- Servers binding to `0.0.0.0` or public interfaces.
- Commands known to bypass the project's verification path.

Post-edit hooks should:

- Format only edited files.
- Use only tools already installed in the repo.
- Never organize imports in a way that breaks split edits.
- Silently skip if the formatter is unavailable.

## Codex and Claude Placement

Codex-relevant project rules must live in tool-neutral files:

- `AGENTS.md`
- `docs/agent-*.md`
- `docs/agent-facts.tsv`
- `tasks/**/*.md`
- `scripts/agent/*`
- `Makefile`

Claude Code-specific files live under `.claude/`. They are adapters, not the source of truth. If a rule or guard matters for all agents, put it in `AGENTS.md`, `docs/`, `tasks/`, or `scripts/agent/`, then let `.claude/` call into that shared logic.

In Codex, `.claude/settings.json` does not automatically trigger hooks. `make check` and CI can prove guard tests and repository invariants, but they cannot retroactively prevent a destructive command from damaging uncommitted workspace state. Pre-execution blocking exists only when the active tool calls the shared guard or enforces an equivalent sandbox. In Claude Code, `.claude/settings.json` can trigger the same guard automatically before Bash and after edits.

## PR or Merge Checklist

Before merging meaningful work:

- The task card is complete.
- Its dependencies and gates are satisfied, and `status` matches the recorded evidence.
- The diff is scoped to the task.
- Tests were added or the no-test reason is explicit.
- Relevant verification commands passed.
- v1 non-goals were not introduced.
- New risks or learned rules were added to docs if durable.
- The final summary names the evidence, not just "works locally."
