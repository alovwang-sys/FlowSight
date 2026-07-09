#!/usr/bin/env python3
"""Conservative command guard for FlowSight agent sessions."""

from __future__ import annotations

import json
import re
import shlex
import sys


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def git_subcommand_args(args: list[str]) -> tuple[str, list[str]] | None:
    if not args or args[0] != "git":
        return None

    index = 1
    while index < len(args):
        token = args[index]
        if token in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            index += 1
            continue
        if token.startswith("--git-dir=") or token.startswith("--work-tree=") or token.startswith("--namespace="):
            index += 1
            continue
        break

    if index >= len(args):
        return None
    return args[index], args[index + 1 :]


def rm_is_dangerous(args: list[str]) -> bool:
    if not args or args[0] != "rm":
        return False

    recursive = False
    force = False
    paths: list[str] = []
    for token in args[1:]:
        if token.startswith("-") and token != "--":
            recursive = recursive or "r" in token.lower()
            force = force or "f" in token.lower()
            continue
        if token == "--":
            continue
        paths.append(token)

    if not (recursive and force):
        return False

    dangerous_paths = {"/", ".", "./", "*", "./*", "~", "$HOME", "$PWD", "${PWD}", "${HOME}"}
    return any(path in dangerous_paths or path.startswith("./*") for path in paths)


def public_bind_requested(args: list[str]) -> bool:
    public_values = {"0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0"}
    for index, token in enumerate(args):
        if token in public_values:
            return True
        if token in {"--host", "--bind", "-b"} and index + 1 < len(args) and args[index + 1] in public_values:
            return True
        if token.startswith("--host=") or token.startswith("--bind="):
            value = token.split("=", 1)[1]
            if value in public_values:
                return True
    return False


def allowed_find_delete(args: list[str]) -> bool:
    if not args or args[0] != "find" or "-delete" not in args:
        return True
    safe_names = {"__pycache__", "*.pyc", "*.pyo"}
    return any(token in safe_names for token in args)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    if payload.get("tool_name") != "Bash":
        return

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command:
        return

    args = argv(command)
    if not args:
        return

    for segment in re.split(r"\s*(?:&&|\|\||;)\s*", command):
        segment_args = argv(segment)
        if not segment_args:
            continue
        git_parts = git_subcommand_args(segment_args)
        if git_parts:
            subcommand, rest = git_parts
            if subcommand == "reset" and "--hard" in rest:
                deny("error: 'git reset --hard' is blocked for agents. Ask the user before destructive git operations.")
            if subcommand == "clean":
                deny("error: 'git clean' is blocked for agents. Ask the user before destructive git operations.")
            if subcommand == "stash":
                deny("error: 'git stash' is blocked for agents. Ask the user before destructive git operations.")
            if subcommand == "checkout" and "--" in rest:
                deny("error: 'git checkout --' is blocked for agents. Ask the user before destructive git operations.")
            if subcommand == "restore" and any(token == "--source" or token.startswith("--source=") for token in rest):
                deny("error: 'git restore --source' is blocked for agents. Ask the user before destructive git operations.")

        if rm_is_dangerous(segment_args):
            deny("error: broad rm -rf command is blocked for agents.")

        if not allowed_find_delete(segment_args):
            deny("error: find -delete is blocked for agents. Use a reviewed, narrow deletion instead.")

        if re.search(r"\bchmod\s+-R\s+777\b", segment):
            deny("error: chmod -R 777 is blocked.")

        if public_bind_requested(segment_args):
            deny("error: FlowSight v1 local servers must bind 127.0.0.1, not a public interface.")

    if "sys.settrace" in command and "global" in command.lower():
        deny("error: FlowSight v1 must not enable global sys.settrace.")


if __name__ == "__main__":
    main()
