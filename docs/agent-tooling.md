# FlowSight Agent Tooling and Cross-Tool Parity

This document maps every rule and guardrail to where it lives, so that FlowSight
behaves the same whether the main developer is Claude Code, Codex, or a human.

## The core idea

Two different agents can never behave byte-for-byte identically, because they
have different native capabilities (Claude Code has in-loop tool hooks; Codex
does not). So the goal is NOT identical agent behavior. The goal is identical
guarantees: the repository's invariants hold no matter which agent is used.

We keep merge-time enforcement out of any single agent and on git/CI. This makes
the committed repository outcome tool-neutral; it does not make pre-execution
command safety equivalent across tools.

## Three layers

### Layer A - Shared source of truth (both tools read natively)

- `AGENTS.md`                     - mandatory rules, v1 boundaries (Codex + Claude read this)
- `CLAUDE.md`                     - one-line pointer to `AGENTS.md`
- `docs/*.md`, `docs/agent-facts.tsv` - design, invariants, workflows
- `docs/workflows/*.md`           - canonical role SOPs (start-task, review, fix, verify, governor)
- `tasks/**/*.md`                 - task cards
- `scripts/agent/*`               - canonical guard + formatter logic
- `Makefile`                      - `make check` and friends

Layer A is already tool-neutral. It is equivalent by construction.

### Layer B - Neutral merge choke points

- `.githooks/pre-commit`          - runs `make check` on every commit (enable with `make init`)
- `.github/workflows/agent-checks.yml` - runs `make check` on push / PR

Both run the SAME `scripts/agent/*` and `make check` regardless of agent. This is
where tested repository invariants are enforced before merge. These checks do not
undo destructive commands, recover untracked files, or prove a phase gate whose
task cards are still planned.

### Layer C - Per-tool adapters (convenience / early feedback only)

- Claude Code: `.claude/settings.json` + `.claude/hooks/*` -> call `scripts/agent/*`
               `.claude/commands/*`   -> thin pointers to `docs/workflows/*`
- Codex:       `codex.config.example.toml` (sandbox + approval + no network)
               relies on AGENTS.md (native) + Layer B for hard enforcement

Layer C differs between tools in timing and command-safety outcome:
- Claude Code can block a dangerous command in-loop (before it runs).
- Codex relies on its active sandbox/permissions and explicit adherence to
  `AGENTS.md`; commit/CI can catch resulting repository violations but cannot
  retroactively block the command.

## The one irreducible difference

In-loop, pre-execution command blocking exists only when the active client calls
the shared guard or provides an equivalent sandbox. Under another client, a
workspace-damaging command could run before any commit/CI gate. Mitigate with the
client's sandbox settings, narrow task allowlists, review, and backups; do not
claim git can restore untracked or otherwise uncommitted user work.

## Switching main developer

Because enforcement lives in Layers A + B, switching is close to zero-cost:

1. Point the other tool at the same repo. No migration needed.
2. Claude Code auto-loads `.claude/`. Codex auto-loads `AGENTS.md`; apply
   `codex.config.example.toml` once.
3. Run `make init` once per clone so git hooks are active for whoever commits.
4. CI already enforces `make check` for everyone.

## Per-rule placement cheat sheet

| Rule type                                   | Put it in                              | Enforced for both by |
|---------------------------------------------|----------------------------------------|----------------------|
| Product / scope / v1 boundary               | `AGENTS.md`, `docs/agent-facts.tsv`    | review + CI          |
| Machine-checkable invariant                 | a test + `docs/agent-facts.tsv`        | `make check` (Layer B) |
| Phase readiness                             | fixed gate opener map + task metadata  | `make gate-phase0/1/4` |
| Forbidden shell command                     | `scripts/agent/pre_bash_guard.py`      | Claude hook + `make guard` + Codex sandbox |
| Forbidden code pattern (e.g. 0.0.0.0 bind)  | a test/linter run by `make check`      | Layer B              |
| Role workflow / SOP                         | `docs/workflows/*.md`                  | both tools read it   |
| Task boundary (allowed/forbidden files)     | the task card in `tasks/`              | review + governor    |

A pre-scaffold `make check` validates only this agent system and must say so in its output. It does not open a phase gate. Each gate target first runs `make check`, then requires its canonical opener tasks, positive spike decisions, `command:` fact verification, and existing test paths; it intentionally fails while any of those remain planned.

See `docs/agent-rule-authoring.md` for the step-by-step protocol when adding a
new rule.
