#!/usr/bin/env python3
"""Smoke tests for task metadata and phase-gate validation."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "validate_agent_system.py"
SPEC = importlib.util.spec_from_file_location("validate_agent_system", VALIDATOR)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ValidatorTests(unittest.TestCase):
    COMPLETE_BODY = """## Related Fact IDs

- FS-999

## Acceptance Criteria

- [x] Observable behavior is verified

## Role Outputs

Implementer:
- implemented the bounded slice

Adversarial Reviewer:
- Reviewer 1: no findings
- Reviewer 2: waived: low-risk test fixture

Fixer:
- no accepted findings

Quality Governor:
- no process drift

## Verifier Evidence

- Command: `make check`
- Result: passed
- Notes: clean test run

## Failure Queue Items

- none
"""

    def test_current_task_graph_is_structurally_valid(self) -> None:
        cards = MODULE.validate_task_cards()
        self.assertEqual(
            {f"TRIAL-{number:03d}" for number in range(1, 6)},
            {task_id for task_id in cards if task_id.startswith("TRIAL-")},
        )

    def test_planned_opener_keeps_isolated_gate_closed(self) -> None:
        cards = {
            "TASK-1": (
                Path("task.md"),
                "",
                {"opens_gates": ["demo-gate"], "status": "planned"},
            )
        }
        MODULE.GATE_OPENERS["demo-gate"] = {"TASK-1"}
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MODULE.validate_gate("demo-gate", cards)
        finally:
            del MODULE.GATE_OPENERS["demo-gate"]

    def test_complete_gate_can_pass(self) -> None:
        cards = {
            "TASK-1": (
                Path("task.md"),
                "",
                {"opens_gates": ["demo-gate"], "status": "complete"},
            )
        }
        MODULE.GATE_OPENERS["demo-gate"] = {"TASK-1"}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.validate_gate("demo-gate", cards)
        finally:
            del MODULE.GATE_OPENERS["demo-gate"]

    def test_complete_task_requires_concrete_role_outputs(self) -> None:
        body = self.COMPLETE_BODY.replace("- implemented the bounded slice", "- TBD")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                MODULE.validate_complete_task("TASK-1", body)

    def test_complete_task_with_evidence_can_pass(self) -> None:
        MODULE.validate_complete_task("TASK-1", self.COMPLETE_BODY)

    def test_gate_rejects_planned_only_fact_evidence(self) -> None:
        cards = {
            "TASK-1": (
                Path("task.md"),
                self.COMPLETE_BODY,
                {"opens_gates": ["demo-gate"], "status": "complete"},
            )
        }
        facts = {
            "FS-999": {
                "verification": "planned:demo",
                "related_tests": "manual:review",
            }
        }
        MODULE.GATE_OPENERS["demo-gate"] = {"TASK-1"}
        MODULE.GATE_FACTS["demo-gate"] = {"FS-999"}
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MODULE.validate_gate("demo-gate", cards, facts)
        finally:
            del MODULE.GATE_OPENERS["demo-gate"]
            del MODULE.GATE_FACTS["demo-gate"]

    def test_task_cannot_declare_an_unknown_gate_opener(self) -> None:
        cards = {
            "TASK-1": (
                Path("task.md"),
                "",
                {"opens_gates": ["invented-gate"], "status": "planned"},
            )
        }
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                MODULE.validate_gate_membership(cards)

    def test_no_go_spike_keeps_gate_closed(self) -> None:
        cards = {
            "TASK-1": (
                Path("task.md"),
                self.COMPLETE_BODY,
                {
                    "opens_gates": ["demo-gate"],
                    "status": "complete",
                    "task_type": "spike",
                    "spike_decision": "no-go",
                },
            )
        }
        MODULE.GATE_OPENERS["demo-gate"] = {"TASK-1"}
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MODULE.validate_gate("demo-gate", cards)
        finally:
            del MODULE.GATE_OPENERS["demo-gate"]

    def test_active_task_cannot_bypass_planned_gate_facts(self) -> None:
        cards = {
            "TASK-1": (
                Path("opener.md"),
                self.COMPLETE_BODY,
                {"status": "complete", "task_type": "implementation", "requires_gates": []},
            ),
            "TASK-2": (
                Path("downstream.md"),
                "",
                {
                    "status": "in_progress",
                    "task_type": "implementation",
                    "requires_gates": ["demo-gate"],
                },
            ),
        }
        facts = {
            "FS-999": {
                "verification": "planned:demo",
                "related_tests": "planned:tests/demo.py",
            }
        }
        MODULE.GATE_OPENERS["demo-gate"] = {"TASK-1"}
        MODULE.GATE_FACTS["demo-gate"] = {"FS-999"}
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MODULE.validate_active_required_gates(cards, facts)
        finally:
            del MODULE.GATE_OPENERS["demo-gate"]
            del MODULE.GATE_FACTS["demo-gate"]

    def test_gate_and_downstream_accept_real_evidence(self) -> None:
        cards = {
            "TASK-1": (
                Path("task.md"),
                self.COMPLETE_BODY,
                {
                    "status": "complete",
                    "task_type": "implementation",
                    "requires_gates": [],
                },
            ),
            "TASK-2": (
                Path("downstream.md"),
                "",
                {
                    "status": "in_progress",
                    "task_type": "implementation",
                    "requires_gates": ["demo-gate"],
                },
            ),
        }
        facts = {
            "FS-999": {
                "verification": "command:make check",
                "related_tests": "tests/test_demo.py::test_evidence",
            }
        }
        old_root = MODULE.ROOT
        MODULE.GATE_OPENERS["demo-gate"] = {"TASK-1"}
        MODULE.GATE_FACTS["demo-gate"] = {"FS-999"}
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                MODULE.ROOT = Path(temp_dir)
                test_path = MODULE.ROOT / "tests" / "test_demo.py"
                test_path.parent.mkdir()
                test_path.write_text("def test_evidence(): pass\n", encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.validate_gate("demo-gate", cards, facts)
                MODULE.validate_active_required_gates(cards, facts)
        finally:
            MODULE.ROOT = old_root
            del MODULE.GATE_OPENERS["demo-gate"]
            del MODULE.GATE_FACTS["demo-gate"]

    def test_cli_reports_current_gate_state(self) -> None:
        cards = MODULE.validate_task_cards()
        facts = MODULE.validate_facts()
        blockers = MODULE.gate_blockers("phase4-tracepoint", cards, facts)
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), "--gate", "phase4-tracepoint"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if blockers:
            self.assertEqual(1, result.returncode)
            self.assertIn("gate 'phase4-tracepoint' is closed", result.stderr)
        else:
            self.assertEqual(0, result.returncode)
            self.assertIn("gate phase4-tracepoint passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
