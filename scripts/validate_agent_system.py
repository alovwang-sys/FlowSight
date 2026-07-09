#!/usr/bin/env python3
"""Validate the FlowSight agent operating-system files."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE_OPENERS = {
    "phase0-sustained": {"TRIAL-001", "TRIAL-002", "TRIAL-004"},
    "phase1-runtime-ingest": {"TRIAL-003", "TRIAL-004"},
    "phase4-tracepoint": {"TRIAL-003", "TRIAL-005"},
}
GATE_FACTS = {
    "phase0-sustained": {"FS-007", "FS-008", "FS-009", "FS-010"},
    "phase1-runtime-ingest": {
        "FS-008",
        "FS-010",
        "FS-018",
        "FS-019",
        "FS-021",
        "FS-025",
        "FS-029",
        "FS-035",
        "FS-036",
    },
    "phase4-tracepoint": {"FS-015", "FS-016", "FS-017", "FS-020", "FS-029", "FS-033"},
}


def fail(message: str) -> None:
    print(f"agent-system validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require_contains(path: str, needles: list[str]) -> None:
    text = read_text(path)
    for needle in needles:
        if needle not in text:
            fail(f"{path} is missing required text: {needle}")


def validate_facts() -> dict[str, dict[str, str]]:
    path = ROOT / "docs" / "agent-facts.tsv"
    required = [
        "fact_id",
        "phase",
        "area",
        "invariant",
        "source",
        "evidence_ref",
        "verification",
        "related_tests",
        "status",
    ]
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != required:
            fail(f"docs/agent-facts.tsv headers must be exactly: {required}")
        seen: set[str] = set()
        rows = list(reader)
    if not rows:
        fail("docs/agent-facts.tsv must contain at least one fact")
    facts: dict[str, dict[str, str]] = {}
    for row in rows:
        fact_id = row["fact_id"]
        if fact_id in seen:
            fail(f"duplicate fact_id: {fact_id}")
        seen.add(fact_id)
        if ":" not in row["evidence_ref"]:
            fail(f"{fact_id} evidence_ref must include a file:line reference")
        if row["status"] not in {"active", "experimental", "retired"}:
            fail(f"{fact_id} has invalid status {row['status']!r}")
        for field in ("source", "evidence_ref", "verification", "related_tests"):
            if not row[field] or row[field].strip().upper() == "TBD":
                fail(f"{fact_id} {field} must contain real or planned evidence, not TBD")
        if not row["verification"].startswith(("planned:", "command:", "manual:")):
            fail(f"{fact_id} verification must start with planned:, command:, or manual:")
        if not row["verification"].split(":", 1)[1].strip():
            fail(f"{fact_id} verification evidence cannot be empty")
        if not (
            row["related_tests"].startswith(("planned:", "manual:"))
            or row["related_tests"].startswith(("tests/", "spikes/"))
        ):
            fail(f"{fact_id} related_tests must be planned/manual evidence or a test path")
        if row["related_tests"].startswith(("planned:", "manual:")):
            if not row["related_tests"].split(":", 1)[1].strip():
                fail(f"{fact_id} related_tests evidence cannot be empty")
        else:
            for reference in row["related_tests"].split(","):
                test_path = reference.strip().split("::", 1)[0]
                if not test_path or not (ROOT / test_path).is_file():
                    fail(f"{fact_id} related test does not exist: {test_path or '<empty>'}")
        source = ROOT / row["source"]
        if not source.exists():
            fail(f"{fact_id} source does not exist: {row['source']}")
        evidence_path = row["evidence_ref"].split(":", 1)[0]
        if not (ROOT / evidence_path).exists():
            fail(f"{fact_id} evidence file does not exist: {evidence_path}")
        facts[fact_id] = row
    return facts


def section(body: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        fail(f"task is missing section {heading!r}")
    return match.group(1)


def concrete_value(value: str, label: str) -> str:
    value = value.strip().strip("`")
    if not value or value.upper() == "TBD" or value.lower() == "pending" or "<" in value:
        fail(f"complete task must provide concrete {label}")
    return value


def validate_complete_task(task_id: str, body: str) -> None:
    if "- [ ]" in body or "TBD" in body:
        fail(f"{task_id} is complete but still has unchecked acceptance or TBD evidence")
    if not re.search(r"^- \[[xX]\] ", section(body, "Acceptance Criteria"), re.MULTILINE):
        fail(f"{task_id} is complete without any checked acceptance criterion")

    roles = section(body, "Role Outputs")
    patterns = {
        "Implementer output": r"^Implementer:\s*\n-\s*(.+)$",
        "Reviewer 1 output": r"^-\s*Reviewer 1:\s*(.+)$",
        "Reviewer 2 output": r"^-\s*Reviewer 2:\s*(.+)$",
        "Fixer output": r"^Fixer:\s*\n-\s*(.+)$",
        "Quality Governor output": r"^Quality Governor:\s*\n-\s*(.+)$",
    }
    for label, pattern in patterns.items():
        match = re.search(pattern, roles, re.MULTILINE)
        if not match:
            fail(f"{task_id} is complete without {label}")
        value = concrete_value(match.group(1), f"{task_id} {label}")
        if label == "Reviewer 2 output" and value.lower().startswith("not required"):
            fail(f"{task_id} Reviewer 2 waiver must use 'waived: <reason>'")
        if label == "Reviewer 2 output" and value.lower().startswith("waived:"):
            concrete_value(value.split(":", 1)[1], f"{task_id} Reviewer 2 waiver reason")

    verifier = section(body, "Verifier Evidence")
    command = re.search(r"^- Command:\s*(.+)$", verifier, re.MULTILINE)
    result = re.search(r"^- Result:\s*(.+)$", verifier, re.MULTILINE)
    notes = re.search(r"^- Notes:\s*(.+)$", verifier, re.MULTILINE)
    if not command or not result or not notes:
        fail(f"{task_id} is complete without full Verifier Evidence")
    concrete_value(command.group(1), f"{task_id} verifier command")
    if concrete_value(result.group(1), f"{task_id} verifier result").lower() not in {"pass", "passed"}:
        fail(f"{task_id} is complete without a passing Verifier Evidence result")
    concrete_value(notes.group(1), f"{task_id} verifier notes")


def related_fact_ids(body: str) -> set[str]:
    return set(re.findall(r"^-\s+(FS-\d+)\s*$", section(body, "Related Fact IDs"), re.MULTILINE))


def validate_task_fact_refs(
    cards: dict[str, tuple[Path, str, dict[str, str | list[str]]]],
    facts: dict[str, dict[str, str]],
) -> None:
    for task_id, (_, body, _) in cards.items():
        fact_ids = related_fact_ids(body)
        if not fact_ids:
            fail(f"{task_id} must reference at least one fact ID")
        missing = fact_ids - facts.keys()
        if missing:
            fail(f"{task_id} references missing facts: {sorted(missing)}")


def validate_gate_membership(
    cards: dict[str, tuple[Path, str, dict[str, str | list[str]]]],
) -> None:
    for task_id, (_, _, metadata) in cards.items():
        opens_gates = metadata["opens_gates"]
        assert isinstance(opens_gates, list)
        unknown = set(opens_gates) - GATE_OPENERS.keys()
        if unknown:
            fail(f"{task_id} opens unknown gates: {sorted(unknown)}")
    for gate, expected_openers in GATE_OPENERS.items():
        declared_openers = {
            task_id
            for task_id, (_, _, metadata) in cards.items()
            if gate in metadata["opens_gates"]
        }
        if declared_openers != expected_openers:
            fail(
                f"gate {gate!r} opener mismatch: expected {sorted(expected_openers)}, "
                f"declared {sorted(declared_openers)}"
            )
        declared_facts = set().union(*(related_fact_ids(cards[task_id][1]) for task_id in expected_openers))
        missing_facts = GATE_FACTS[gate] - declared_facts
        if missing_facts:
            fail(f"gate {gate!r} opener cards omit canonical facts: {sorted(missing_facts)}")


def parse_list(value: str) -> list[str]:
    value = value.strip()
    if value == "[]":
        return []
    if not (value.startswith("[") and value.endswith("]")):
        fail(f"task metadata list must use [item, item] syntax: {value!r}")
    return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]


def parse_task_metadata(path: Path, body: str) -> dict[str, str | list[str]]:
    match = re.search(r"## Task Metadata\s+```yaml\s+(.*?)\s+```", body, re.DOTALL)
    if not match:
        fail(f"{path.relative_to(ROOT)} is missing a fenced Task Metadata block")
    metadata: dict[str, str | list[str]] = {}
    list_fields = {"impacted_phases", "depends_on", "requires_gates", "opens_gates"}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            fail(f"{path.relative_to(ROOT)} has invalid metadata line: {raw_line!r}")
        key, value = line.split(":", 1)
        value = value.split("  #", 1)[0].strip()
        metadata[key.strip()] = parse_list(value) if key.strip() in list_fields else value.strip("'\"")
    required = {
        "task_id",
        "release",
        "task_type",
        "status",
        "primary_phase",
        "impacted_phases",
        "depends_on",
        "requires_gates",
        "opens_gates",
        "scope_override",
        "scope_override_approved_by",
    }
    missing = required - metadata.keys()
    if missing:
        fail(f"{path.relative_to(ROOT)} metadata is missing {sorted(missing)}")
    return metadata


def load_task_cards() -> dict[str, tuple[Path, str, dict[str, str | list[str]]]]:
    tasks = sorted((ROOT / "tasks").rglob("*.md"))
    if not tasks:
        fail("tasks must contain task cards")
    required = [
        "Task Metadata",
        "Task ID",
        "Related Fact IDs",
        "Allowed Files",
        "Acceptance Criteria",
        "Verification",
        "Role Outputs",
        "Verifier Evidence",
        "Failure Queue Items",
    ]
    cards: dict[str, tuple[Path, str, dict[str, str | list[str]]]] = {}
    for task in tasks:
        body = task.read_text(encoding="utf-8")
        for needle in required:
            if needle not in body:
                fail(f"{task.relative_to(ROOT)} is missing {needle}")
        metadata = parse_task_metadata(task, body)
        task_id = str(metadata["task_id"])
        task_id_section = re.search(r"## Task ID\s+([^\s]+)", body)
        if not task_id_section or task_id_section.group(1) != task_id:
            fail(f"{task.relative_to(ROOT)} Task ID section does not match metadata {task_id}")
        phase_section = re.search(r"## Phase\s+Phase ([0-6])", body)
        expected_phase = str(metadata["primary_phase"]).removeprefix("phase")
        if not phase_section or phase_section.group(1) != expected_phase:
            fail(f"{task.relative_to(ROOT)} Phase section does not match metadata")
        if task_id in cards:
            fail(f"duplicate task_id: {task_id}")
        cards[task_id] = (task, body, metadata)
    expected_trials = {f"TRIAL-{number:03d}" for number in range(1, 6)}
    missing_trials = expected_trials - cards.keys()
    if missing_trials:
        fail(f"missing pre-implementation task cards: {sorted(missing_trials)}")
    return cards


def validate_task_cards() -> dict[str, tuple[Path, str, dict[str, str | list[str]]]]:
    cards = load_task_cards()
    allowed_statuses = {"planned", "in_progress", "review", "blocked", "complete"}
    allowed_releases = {"v1", "v1.1-experimental"}
    allowed_types = {"implementation", "spike", "safety", "tooling", "docs"}
    allowed_phases = {f"phase{number}" for number in range(7)}

    for task_id, (_path, body, metadata) in cards.items():
        status = str(metadata["status"])
        release = str(metadata["release"])
        task_type = str(metadata["task_type"])
        phase = str(metadata["primary_phase"])
        if status not in allowed_statuses:
            fail(f"{task_id} has invalid status {status!r}")
        if release not in allowed_releases:
            fail(f"{task_id} has invalid release {release!r}")
        if task_type not in allowed_types:
            fail(f"{task_id} has invalid task_type {task_type!r}")
        if task_type == "spike":
            decision = metadata.get("spike_decision")
            reductions = metadata.get("scope_reductions")
            if decision not in {"pending", "go", "go-with-scope-reductions", "no-go"}:
                fail(f"{task_id} spike_decision is missing or invalid")
            if not reductions:
                fail(f"{task_id} scope_reductions is required for a spike")
            if status == "complete" and decision == "pending":
                fail(f"{task_id} cannot complete without a spike decision")
            if status == "complete" and reductions == "pending":
                fail(f"{task_id} cannot complete without a scope-reduction outcome")
            if status == "complete" and decision == "go-with-scope-reductions" and reductions in {"none", "pending"}:
                fail(f"{task_id} must record concrete scope reductions")
            if (
                status == "complete"
                and decision == "go-with-scope-reductions"
                and metadata["scope_override"] == "none"
            ):
                fail(f"{task_id} scope reductions require an explicit approved scope override")
        if phase not in allowed_phases:
            fail(f"{task_id} has invalid primary_phase {phase!r}")
        if release == "v1" and phase == "phase6":
            fail(f"{task_id}: phase6 tasks must use release v1.1-experimental")
        if metadata["scope_override"] != "none" and metadata["scope_override_approved_by"] == "none":
            fail(f"{task_id}: scope override requires explicit approval")
        dependencies = metadata["depends_on"]
        assert isinstance(dependencies, list)
        for dependency in dependencies:
            if dependency == task_id:
                fail(f"{task_id} cannot depend on itself")
            if dependency not in cards:
                fail(f"{task_id} depends on missing task {dependency}")
            dependency_status = str(cards[dependency][2]["status"])
            if status in {"in_progress", "review", "complete"} and dependency_status != "complete":
                fail(f"{task_id} is {status} but dependency {dependency} is {dependency_status}")
        if status == "complete":
            validate_complete_task(task_id, body)

    validate_gate_membership(cards)

    for task_id, (_, _, metadata) in cards.items():
        for gate in metadata["requires_gates"]:
            if gate not in GATE_OPENERS:
                fail(f"{task_id} requires unknown gate {gate!r}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            fail(f"task dependency cycle includes {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        dependencies = cards[task_id][2]["depends_on"]
        assert isinstance(dependencies, list)
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in cards:
        visit(task_id)
    return cards


def gate_blockers(
    gate: str,
    cards: dict[str, tuple[Path, str, dict[str, str | list[str]]]],
    facts: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    if gate not in GATE_OPENERS:
        fail(f"unknown gate {gate!r}")
    blockers: list[str] = []
    for task_id in sorted(GATE_OPENERS[gate]):
        status = str(cards[task_id][2]["status"])
        if status != "complete":
            blockers.append(f"{task_id}={status}")
        metadata = cards[task_id][2]
        if metadata.get("task_type") == "spike" and metadata.get("spike_decision") not in {
            "go",
            "go-with-scope-reductions",
        }:
            blockers.append(f"{task_id}.spike_decision={metadata.get('spike_decision', 'missing')}")
    if facts is None:
        return blockers
    for fact_id in sorted(GATE_FACTS[gate]):
        fact = facts.get(fact_id)
        if fact is None:
            blockers.append(f"{fact_id}=missing")
            continue
        if not fact["verification"].startswith("command:"):
            blockers.append(f"{fact_id}.verification={fact['verification']}")
        else:
            command = fact["verification"].removeprefix("command:").strip()
            if not re.match(r"^(make (check|test(?:-[a-z0-9-]+)?)|python3? -m pytest\b)", command):
                blockers.append(f"{fact_id}.verification_command={command or '<empty>'}")
        related_tests = fact["related_tests"]
        if related_tests.startswith(("planned:", "manual:")):
            blockers.append(f"{fact_id}.related_tests={related_tests}")
            continue
        for reference in related_tests.split(","):
            test_path = reference.strip().split("::", 1)[0]
            if not test_path.startswith("tests/") or not (ROOT / test_path).is_file():
                blockers.append(f"{fact_id}.related_tests_missing={test_path or '<empty>'}")
    return blockers


def validate_active_required_gates(
    cards: dict[str, tuple[Path, str, dict[str, str | list[str]]]],
    facts: dict[str, dict[str, str]],
) -> None:
    for task_id, (_, _, metadata) in cards.items():
        status = str(metadata["status"])
        if status not in {"in_progress", "review", "complete"}:
            continue
        for gate in metadata["requires_gates"]:
            blockers = gate_blockers(gate, cards, facts)
            if blockers:
                fail(
                    f"{task_id} is {status} while required gate {gate!r} is closed: "
                    f"{', '.join(blockers)}"
                )


def validate_gate(
    gate: str,
    cards: dict[str, tuple[Path, str, dict[str, str | list[str]]]],
    facts: dict[str, dict[str, str]] | None = None,
) -> None:
    blockers = gate_blockers(gate, cards, facts)
    if blockers:
        fail(f"gate {gate!r} is closed: {', '.join(blockers)}")
    print(f"gate {gate} passed")


def validate_no_tracked_pycache() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    tracked = [Path(path) for path in result.stdout.decode().split("\0") if path]
    generated = [
        str(path)
        for path in tracked
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}
    ]
    if generated:
        fail(f"generated Python cache files must not be tracked: {generated}")


def validate_python_syntax() -> None:
    paths = [
        ".claude/hooks/pre-bash-guard.py",
        ".claude/hooks/post-edit-format.py",
        "scripts/agent/pre_bash_guard.py",
        "scripts/agent/post_edit_format.py",
        "scripts/agent/test_pre_bash_guard.py",
        "scripts/agent/test_validate_agent_system.py",
        "scripts/validate_agent_system.py",
    ]
    for path in paths:
        full_path = ROOT / path
        try:
            compile(full_path.read_text(encoding="utf-8"), str(full_path), "exec")
        except SyntaxError as error:
            fail(f"{path} has invalid Python syntax: {error}")



def validate_tool_parity() -> None:
    commands_dir = ROOT / ".claude" / "commands"
    workflows_dir = ROOT / "docs" / "workflows"
    workflows = {path.stem for path in workflows_dir.glob("*.md")}
    if not workflows:
        fail("docs/workflows must contain the canonical role SOP files")
    commands = {path.stem for path in commands_dir.glob("*.md")}
    missing_workflow = commands - workflows
    if missing_workflow:
        fail(f"each .claude/command must mirror a docs/workflow: missing {sorted(missing_workflow)}")
    missing_command = workflows - commands
    if missing_command:
        fail(f"each docs/workflow must have a .claude/command pointer: missing {sorted(missing_command)}")

    pre_commit = ROOT / ".githooks" / "pre-commit"
    if not pre_commit.exists():
        fail(".githooks/pre-commit must exist so git enforces make check for all tools")
    if "make check" not in pre_commit.read_text(encoding="utf-8"):
        fail(".githooks/pre-commit must run 'make check'")

    workflows_ci = list((ROOT / ".github" / "workflows").glob("*.yml")) + list(
        (ROOT / ".github" / "workflows").glob("*.yaml")
    )
    if not any("make check" in path.read_text(encoding="utf-8") for path in workflows_ci):
        fail(".github/workflows must contain a workflow that runs 'make check'")

    if not (ROOT / "codex.config.example.toml").exists():
        fail("codex.config.example.toml must exist so Codex parity is documented and versioned")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", help="Require every task participating in this gate to be complete")
    args = parser.parse_args()
    validate_python_syntax()
    require_contains(
        "AGENTS.md",
        [
            "docs/agent-facts.tsv",
            "docs/agent-trial-plan.md",
            "make check",
            "scope override",
            "scripts/agent/",
            "docs/agent-tooling.md",
            "docs/workflows/",
            "sidecar",
            "Trial 5",
        ],
    )
    require_contains(
        "docs/agent-operating-system.md",
        [
            "scripts/agent/pre_bash_guard.py",
            "Codex-relevant project rules",
            "Claude Code-specific files live under `.claude/`",
        ],
    )
    require_contains(
        "docs/agent-task-template.md",
        [
            "Task ID",
            "primary_phase",
            "depends_on",
            "requires_gates",
            "opens_gates",
            "scope_override",
            "Related Fact IDs",
            "Expected Changed Files",
            "No-Test Reason",
            "Role Outputs",
            "Verifier Evidence",
            "Failure Queue Items",
        ],
    )
    require_contains(
        ".github/pull_request_template.md",
        [
            "Task Card",
            "Related Fact IDs",
            "Role Sign-off",
            "Failure Queue Items",
        ],
    )
    require_contains("docs/agent-failure-queue.md", ["queue/failures.jsonl", "open -> claimed -> fixed -> verified"])
    facts = validate_facts()
    cards = validate_task_cards()
    validate_task_fact_refs(cards, facts)
    validate_active_required_gates(cards, facts)
    validate_tool_parity()
    validate_no_tracked_pycache()
    if args.gate:
        validate_gate(args.gate, cards, facts)
    print("agent-system validation passed")


if __name__ == "__main__":
    main()
