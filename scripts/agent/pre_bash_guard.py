#!/usr/bin/env python3
"""Conservative command guard for FlowSight agent sessions."""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL_EXECUTABLES = {"bash", "dash", "ksh", "sh", "zsh"}
SERVER_EXECUTABLES = {"fastapi", "flask", "gunicorn", "hypercorn", "uvicorn", "vite"}
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(.*)$", re.DOTALL)
DYNAMIC_VALUE_MARKER = "__FLOWSIGHT_DYNAMIC_SHELL_VALUE__"
CONTROL_PREFIXES = {
    "!",
    "{",
    "case",
    "do",
    "elif",
    "else",
    "for",
    "function",
    "if",
    "select",
    "then",
    "time",
    "until",
    "while",
}
CONTROL_TERMINATORS = {"}", "done", "esac", "fi"}
RUN_WRAPPERS = {"pipenv", "poetry", "uv"}
SENSITIVE_EXECUTABLES = (
    SHELL_EXECUTABLES
    | SERVER_EXECUTABLES
    | {
        "chmod",
        "command",
        "env",
        "exec",
        "find",
        "git",
        "nohup",
        "rm",
        "sudo",
        "xargs",
    }
)
GIT_GLOBAL_FLAGS = {
    "--bare",
    "--glob-pathspecs",
    "--html-path",
    "--icase-pathspecs",
    "--info-path",
    "--literal-pathspecs",
    "--man-path",
    "--no-lazy-fetch",
    "--no-optional-locks",
    "--no-pager",
    "--no-replace-objects",
    "--noglob-pathspecs",
    "--paginate",
    "-P",
    "-p",
}
GIT_GLOBAL_OPTIONS_WITH_VALUES = {
    "--git-dir",
    "--namespace",
    "--super-prefix",
    "--work-tree",
}
GIT_BUILTIN_COMMANDS = {
    "add",
    "am",
    "apply",
    "archive",
    "bisect",
    "blame",
    "branch",
    "bundle",
    "cat-file",
    "check-attr",
    "check-ignore",
    "check-mailmap",
    "check-ref-format",
    "checkout",
    "checkout-index",
    "cherry",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "config",
    "count-objects",
    "describe",
    "diff",
    "difftool",
    "fetch",
    "for-each-ref",
    "format-patch",
    "fsck",
    "gc",
    "grep",
    "hash-object",
    "init",
    "log",
    "ls-files",
    "ls-remote",
    "ls-tree",
    "maintenance",
    "merge",
    "merge-base",
    "mergetool",
    "mv",
    "notes",
    "pull",
    "push",
    "range-diff",
    "read-tree",
    "rebase",
    "reflog",
    "remote",
    "replace",
    "reset",
    "restore",
    "rev-list",
    "rev-parse",
    "revert",
    "rm",
    "show",
    "show-branch",
    "sparse-checkout",
    "stash",
    "status",
    "submodule",
    "switch",
    "symbolic-ref",
    "tag",
    "update-index",
    "update-ref",
    "verify-commit",
    "verify-tag",
    "version",
    "worktree",
    "write-tree",
}


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


def executable_name(token: str) -> str:
    """Return a command's basename without requiring the executable to exist."""

    return token.replace("\\", "/").rsplit("/", 1)[-1]


def long_option_matches(token: str, full_option: str) -> bool:
    """Match Git-style unique long-option prefixes conservatively."""

    candidate = token.split("=", 1)[0]
    return candidate == full_option or (
        candidate.startswith("--")
        and len(candidate) > 2
        and full_option.startswith(candidate)
    )


def short_option_contains(token: str, option: str) -> bool:
    return (
        token.startswith("-")
        and not token.startswith("--")
        and option in token[1:]
    )


def extract_command_substitutions(command: str) -> tuple[str, list[str]]:
    """Replace command substitutions and return their scripts for recursive review."""

    output: list[str] = []
    scripts: list[str] = []
    quote: str | None = None
    index = 0

    while index < len(command):
        char = command[index]
        if quote == "'":
            output.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            output.append(command[index : index + 2])
            index += 2
            continue
        if char == "'" and quote is None:
            quote = "'"
            output.append(char)
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            output.append(char)
            index += 1
            continue

        if command.startswith("$(", index) and not command.startswith("$((", index):
            start = index + 2
            cursor = start
            depth = 1
            nested_quote: str | None = None
            while cursor < len(command):
                nested_char = command[cursor]
                if nested_quote == "'":
                    if nested_char == "'":
                        nested_quote = None
                    cursor += 1
                    continue
                if nested_char == "\\" and cursor + 1 < len(command):
                    cursor += 2
                    continue
                if nested_char in {"'", '"'}:
                    if nested_quote is None:
                        nested_quote = nested_char
                    elif nested_quote == nested_char:
                        nested_quote = None
                    cursor += 1
                    continue
                if nested_quote is None and command.startswith("$(", cursor):
                    depth += 1
                    cursor += 2
                    continue
                if nested_quote is None and nested_char == "(":
                    depth += 1
                elif nested_quote is None and nested_char == ")":
                    depth -= 1
                    if depth == 0:
                        break
                cursor += 1
            if depth != 0:
                deny("error: unclosed command substitution cannot be reviewed safely.")
            scripts.append(command[start:cursor])
            output.append(DYNAMIC_VALUE_MARKER)
            index = cursor + 1
            continue

        if char == "`":
            cursor = index + 1
            while cursor < len(command):
                if command[cursor] == "\\" and cursor + 1 < len(command):
                    cursor += 2
                    continue
                if command[cursor] == "`":
                    break
                cursor += 1
            if cursor >= len(command):
                deny("error: unclosed backtick command substitution cannot be reviewed safely.")
            scripts.append(command[index + 1 : cursor])
            output.append(DYNAMIC_VALUE_MARKER)
            index = cursor + 1
            continue

        output.append(char)
        index += 1

    return "".join(output), scripts


def split_shell_segments(command: str) -> list[str]:
    """Split top-level shell commands without splitting quoted command strings."""

    segments: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < len(command):
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in {"\n", ";", "&", "|", "(", ")"}:
            segment = command[start:index].strip()
            if segment:
                segments.append(segment)
            while index + 1 < len(command) and command[index + 1] in {"&", "|"}:
                index += 1
            start = index + 1
        index += 1

    segment = command[start:].strip()
    if segment:
        segments.append(segment)
    return segments


def strip_control_prefixes(args: list[str]) -> list[str]:
    remaining = list(args)
    while remaining and remaining[0] in CONTROL_PREFIXES:
        remaining.pop(0)
    if remaining and remaining[0] in CONTROL_TERMINATORS:
        return []
    return remaining


def unwrap_run_wrapper(args: list[str]) -> list[str] | None:
    if len(args) < 2 or executable_name(args[0]) not in RUN_WRAPPERS or args[1] != "run":
        return None

    tail = args[2:]
    if tail and tail[0] == "--":
        return tail[1:]
    for index, token in enumerate(tail):
        if token == "--" and index + 1 < len(tail):
            return tail[index + 1 :]
        if ASSIGNMENT_RE.match(token):
            return tail[index:]
        name = executable_name(token)
        if name in SENSITIVE_EXECUTABLES or name.startswith("python"):
            return tail[index:]
    return tail


def unwrap_process_wrapper(args: list[str]) -> list[str] | None:
    """Unwrap common process runners so the real executable is inspected."""

    if not args:
        return None
    command = executable_name(args[0])

    if command == "timeout":
        index = 1
        options_with_values = {"-k", "--kill-after", "-s", "--signal"}
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            index += 1
            if option in options_with_values and index < len(args):
                index += 1
        if index < len(args):
            index += 1  # duration
        return args[index:]

    if command == "nice":
        index = 1
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            index += 1
            if option in {"-n", "--adjustment"} and index < len(args):
                index += 1
        return args[index:]

    if command == "npx":
        index = 1
        options_with_values = {"-c", "--call", "-p", "--package"}
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            if (
                (option.startswith("-c") and not option.startswith("--"))
                or long_option_matches(option, "--call")
            ):
                deny("error: npx shell-string execution is opaque to the command guard.")
            index += 1
            if option in options_with_values and index < len(args):
                index += 1
        return args[index:]

    if command == "npm":
        index = 1
        prefix_removed = False
        options_with_values = {"-C", "--prefix", "-w", "--workspace"}
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            index += 1
            prefix_removed = True
            if option in options_with_values and index < len(args):
                index += 1
        if index >= len(args):
            return None
        subcommand = args[index]
        if subcommand in {"exec", "x"}:
            index += 1
            exec_options_with_values = {"-p", "--package", "-w", "--workspace"}
            while index < len(args) and args[index].startswith("-"):
                if args[index] == "--":
                    index += 1
                    break
                option = args[index]
                if (
                    (option.startswith("-c") and not option.startswith("--"))
                    or long_option_matches(option, "--call")
                ):
                    deny("error: npm exec shell-string execution is opaque to the command guard.")
                index += 1
                if option in exec_options_with_values and index < len(args):
                    index += 1
            return args[index:]
        if prefix_removed:
            return [args[0], *args[index:]]

    return None


def normalized_command(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Strip shell assignments and transparent wrappers from one command."""

    remaining = list(args)
    assignments: dict[str, str] = {}
    while remaining:
        while remaining and ASSIGNMENT_RE.match(remaining[0]):
            name, value = remaining.pop(0).split("=", 1)
            assignments[name] = value
        if not remaining:
            break

        command = executable_name(remaining[0])
        if command == "env":
            remaining.pop(0)
            while remaining and remaining[0].startswith("-"):
                option = remaining.pop(0)
                if option == "-S" or option.startswith("-S") or long_option_matches(
                    option, "--split-string"
                ):
                    deny("error: env split-string execution is opaque to the command guard.")
                if option in {"-u", "--unset"} and remaining:
                    remaining.pop(0)
            continue
        if command in {"command", "exec", "nohup"}:
            remaining.pop(0)
            while remaining and remaining[0].startswith("-"):
                remaining.pop(0)
            continue
        run_command = unwrap_run_wrapper(remaining)
        if run_command is not None:
            remaining = run_command
            continue
        process_command = unwrap_process_wrapper(remaining)
        if process_command is not None:
            remaining = process_command
            continue
        break
    return remaining, assignments


def shell_script(args: list[str]) -> str | None:
    if not args or executable_name(args[0]) not in SHELL_EXECUTABLES:
        return None

    for index, token in enumerate(args[1:], start=1):
        if token in {"-c", "--command"} or (
            token.startswith("-") and not token.startswith("--") and "c" in token[1:]
        ):
            if index + 1 < len(args):
                return args[index + 1]
            return None
    return None


def git_subcommand_args(
    args: list[str], cwd: Path
) -> tuple[str, list[str], Path, list[str], list[str]] | None:
    if not args or executable_name(args[0]) != "git":
        return None

    index = 1
    workdir = cwd
    config_overrides: list[str] = []
    config_env_keys: list[str] = []
    while index < len(args):
        token = args[index]
        if token == "-C" and index + 1 < len(args):
            candidate = Path(os.path.expanduser(args[index + 1]))
            workdir = candidate if candidate.is_absolute() else workdir / candidate
            workdir = workdir.resolve(strict=False)
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            candidate = Path(os.path.expanduser(token[2:]))
            workdir = candidate if candidate.is_absolute() else workdir / candidate
            workdir = workdir.resolve(strict=False)
            index += 1
            continue
        if token in GIT_GLOBAL_FLAGS:
            index += 1
            continue
        if token == "-c" and index + 1 < len(args):
            config_overrides.append(args[index + 1])
            index += 2
            continue
        if token.startswith("-c") and token != "-c":
            config_overrides.append(token[2:])
            index += 1
            continue
        if token == "--config-env" and index + 1 < len(args):
            config_env_keys.append(args[index + 1].partition("=")[0])
            index += 2
            continue
        if token.startswith("--config-env="):
            config_env_keys.append(token.split("=", 1)[1].partition("=")[0])
            index += 1
            continue
        if token in GIT_GLOBAL_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if token.startswith(
            (
                "--git-dir=",
                "--namespace=",
                "--super-prefix=",
                "--work-tree=",
            )
        ):
            index += 1
            continue
        break

    if index >= len(args):
        return None
    return args[index], args[index + 1 :], workdir, config_overrides, config_env_keys


def dangerous_git_config_override(config: str) -> bool:
    key = config.split("=", 1)[0].strip().lower()
    return (
        key == "core.hookspath"
        or key.startswith("alias.")
        or key.startswith("include.")
        or key.startswith("includeif.")
    )


def remote_config_forces_push(key: str, value: str | None) -> bool:
    match = re.fullmatch(r"remote\..+\.(mirror|push)", key.strip().lower())
    if match is None:
        return False
    if match.group(1) == "mirror":
        return value is None or value.strip().lower() not in {"0", "false", "no", "off"}
    return value is not None and value.lstrip().startswith("+")


def git_config_override_forces_push(config: str) -> bool:
    key, separator, value = config.partition("=")
    return remote_config_forces_push(key, value if separator else None)


def git_config_env_may_force_push(key: str) -> bool:
    return re.fullmatch(r"remote\..+\.(mirror|push)", key.strip().lower()) is not None


def git_config_write_forces_push(args: list[str]) -> bool:
    read_actions = {
        "--get",
        "--get-all",
        "--get-color",
        "--get-colorbool",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "-l",
        "get",
        "list",
    }
    non_force_actions = {
        "--edit",
        "--remove-section",
        "--rename-section",
        "--unset",
        "--unset-all",
        "edit",
        "remove-section",
        "rename-section",
        "unset",
    }
    write_actions = {"--add", "--replace-all", "set"}
    options_with_values = {"--blob", "--comment", "--file", "--type", "-f", "-t"}
    positionals: list[str] = []
    parse_options = True
    index = 0

    while index < len(args):
        token = args[index]
        if parse_options and token == "--":
            parse_options = False
            index += 1
            continue
        if parse_options and token in read_actions:
            return False
        if parse_options and token in non_force_actions:
            return False
        if parse_options and token in write_actions:
            index += 1
            continue
        if parse_options and token in options_with_values:
            index += 2
            continue
        if parse_options and token.startswith(("--blob=", "--file=", "--type=")):
            index += 1
            continue
        if parse_options and token.startswith("-") and not positionals:
            index += 1
            continue
        positionals.append(token)
        index += 1

    if len(positionals) < 2:
        return False
    return remote_config_forces_push(positionals[0], positionals[1])


def git_environment_changes_config(assignments: dict[str, str]) -> bool:
    names = {name.upper() for name in assignments}
    return bool(
        names & {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
        or any(name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) for name in names)
    )


def protected_git_config_key(token: str) -> bool:
    key = token.strip().lower()
    return (
        key == "core.hookspath"
        or key.startswith("alias.")
        or key.startswith("include.")
        or key.startswith("includeif.")
    )


def git_config_changes_protected_key(args: list[str]) -> bool:
    """Reject writes/removals that can replace hooks or introduce opaque aliases."""

    protected_indexes = [
        index for index, token in enumerate(args) if protected_git_config_key(token)
    ]
    if not protected_indexes:
        return False

    mutating_options = {
        "--add",
        "--append",
        "--edit",
        "--fixed-value",
        "--rename-section",
        "--remove-section",
        "--replace-all",
        "--unset",
        "--unset-all",
    }
    if any(
        long_option_matches(token, option)
        for token in args
        for option in mutating_options
    ):
        return True

    # `git config key` is a read; a later non-option is a value and therefore a write.
    for key_index in protected_indexes:
        if any(not token.startswith("-") for token in args[key_index + 1 :]):
            return True
    return False


def configured_git_alias(subcommand: str, cwd: Path) -> bool:
    if subcommand in GIT_BUILTIN_COMMANDS:
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", subcommand):
        return True

    git = shutil.which("git")
    if git is None:
        return False
    try:
        result = subprocess.run(
            [git, "-C", str(cwd), "config", "--get", f"alias.{subcommand}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode == 0:
        return bool(result.stdout.strip())
    return result.returncode not in {1}


def commit_skips_hooks(args: list[str]) -> bool:
    options_with_attached_values = {"C", "F", "S", "c", "m", "t", "u"}
    for token in args:
        if long_option_matches(token, "--no-verify"):
            return True
        if not token.startswith("-") or token.startswith("--"):
            continue
        for option in token[1:]:
            if option == "n":
                return True
            if option in options_with_attached_values:
                break
    return False


def push_forces_remote_update(args: list[str]) -> bool:
    force_options = ("--force", "--force-with-lease", "--mirror")
    long_options_with_values = (
        "--exec",
        "--push-option",
        "--receive-pack",
        "--recurse-submodules",
        "--repo",
    )
    positionals: list[str] = []
    repository_from_option = False
    parse_options = True
    index = 0

    while index < len(args):
        token = args[index]
        if parse_options and token == "--":
            parse_options = False
            index += 1
            continue

        if parse_options and token.startswith("--"):
            if any(long_option_matches(token, option) for option in force_options):
                return True

            value_option = next(
                (
                    option
                    for option in long_options_with_values
                    if long_option_matches(token, option)
                ),
                None,
            )
            if value_option is not None:
                repository_from_option = repository_from_option or value_option == "--repo"
                index += 1 if "=" in token else 2
                continue

            index += 1
            continue

        if parse_options and token.startswith("-") and token != "-":
            cluster = token[1:]
            option_index = 0
            while option_index < len(cluster):
                option = cluster[option_index]
                if option == "o":
                    index += 2 if option_index == len(cluster) - 1 else 1
                    break
                if option == "f":
                    return True
                option_index += 1
            else:
                index += 1
            continue

        positionals.append(token)
        index += 1

    refspecs = positionals if repository_from_option else positionals[1:]
    return any(refspec.startswith("+") and len(refspec) > 1 for refspec in refspecs)


def looks_like_checkout_path(token: str, cwd: Path) -> bool:
    if token in {".", "..", "./", "../"}:
        return True
    if token.startswith(("./", "../", "/", "~", ":(")):
        return True
    if any(character in token for character in "*?[\\"):
        return True
    if (cwd / os.path.expanduser(token)).exists():
        return True
    # A missing source file may still be tracked. A suffix is a useful conservative
    # distinction; branch names with dots can use the unambiguous `git switch`.
    return bool(Path(token).suffix)


def checkout_updates_paths(args: list[str], cwd: Path) -> bool:
    path_options = {
        "--conflict",
        "--ours",
        "--pathspec-from-file",
        "--theirs",
        "--worktree",
        "-m",
        "-p",
        "--patch",
    }
    if "--" in args or any(
        token in path_options
        or long_option_matches(token, "--pathspec-from-file")
        or long_option_matches(token, "--worktree")
        or long_option_matches(token, "--patch")
        or long_option_matches(token, "--ours")
        or long_option_matches(token, "--theirs")
        for token in args
    ):
        return True
    if any(
        short_option_contains(token, "f") or long_option_matches(token, "--force")
        for token in args
    ):
        return True

    branch_creation = any(
        token in {"-b", "-B", "--orphan"}
        or token.startswith(("--orphan=",))
        for token in args
    )
    if branch_creation:
        return False

    options_with_values = {"-b", "-B", "--conflict", "--orphan"}
    positionals: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in options_with_values:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1

    if len(positionals) > 1:
        return True
    return len(positionals) == 1 and looks_like_checkout_path(positionals[0], cwd)


def restore_updates_worktree(args: list[str]) -> bool:
    if any(long_option_matches(token, "--source") for token in args):
        return True
    if any(token in {"-h", "--help"} for token in args):
        return False

    staged = any(
        long_option_matches(token, "--staged")
        or (token.startswith("-") and not token.startswith("--") and "S" in token[1:])
        for token in args
    )
    worktree = any(
        long_option_matches(token, "--worktree")
        or (token.startswith("-") and not token.startswith("--") and "W" in token[1:])
        for token in args
    )
    if staged and not worktree:
        return False

    return any(not token.startswith("-") for token in args) or worktree


def switch_discards_changes(args: list[str]) -> bool:
    if any(
        long_option_matches(token, "--discard-changes")
        or long_option_matches(token, "--force")
        for token in args
    ):
        return True
    return any(
        token.startswith("-") and not token.startswith("--") and "f" in token[1:]
        for token in args
    )


def expanded_rm_targets(target: str, cwd: Path) -> list[Path]:
    expanded = target.replace("${PWD}", str(cwd)).replace("$PWD", str(cwd))
    expanded = expanded.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
    expanded = os.path.expanduser(expanded)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = cwd / candidate

    if glob.has_magic(str(candidate)):
        matches = glob.glob(str(candidate), recursive=False)
        return [Path(match).resolve(strict=False) for match in matches]
    return [candidate.resolve(strict=False)]


def is_dynamic_shell_value(value: str) -> bool:
    return DYNAMIC_VALUE_MARKER in value or "$" in value or "`" in value


def broad_rm_pattern(target: str, cwd: Path) -> bool:
    if not glob.has_magic(target):
        return False
    magic_indexes = [index for index, char in enumerate(target) if char in "*?["]
    prefix = target[: min(magic_indexes)]
    directory = prefix.rsplit("/", 1)[0] if "/" in prefix else "."
    directory_path = Path(os.path.expanduser(directory or "/"))
    if not directory_path.is_absolute():
        directory_path = cwd / directory_path
    resolved = directory_path.resolve(strict=False)
    return resolved == cwd or resolved == REPO_ROOT or resolved in REPO_ROOT.parents


def rm_is_dangerous(args: list[str], cwd: Path) -> bool:
    if not args or executable_name(args[0]) != "rm":
        return False

    recursive = False
    force = False
    paths: list[str] = []
    parse_options = True
    for token in args[1:]:
        if parse_options and token == "--":
            parse_options = False
            continue
        if parse_options and token.startswith("-"):
            if token in {"--recursive", "--dir"}:
                recursive = True
            elif token == "--force":
                force = True
            elif not token.startswith("--"):
                recursive = recursive or "r" in token.lower()
                force = force or "f" in token.lower()
            continue
        paths.append(token)

    if not (recursive and force):
        return False

    if any(
        is_dynamic_shell_value(path)
        or broad_rm_pattern(path, cwd)
        or ("{" in path and "}" in path)
        for path in paths
    ):
        return True

    broad_literals = {"/", ".", "./", "..", "../", "*", "./*", "../*", "~", "$HOME", "$PWD", "${PWD}", "${HOME}"}
    if any(path in broad_literals or path.startswith(("./*", "../*")) for path in paths):
        return True

    for target in paths:
        for resolved in expanded_rm_targets(target, cwd):
            if resolved == REPO_ROOT or resolved in REPO_ROOT.parents:
                return True
    return False


def is_loopback_bind_value(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized == "127.0.0.1" or normalized.startswith("127.0.0.1:")


def is_server_command(args: list[str]) -> bool:
    if not args:
        return False
    command = executable_name(args[0])
    if command in SERVER_EXECUTABLES:
        return True
    module = python_module(args)
    if module is not None:
        return module[0] in {"http.server", "uvicorn", "hypercorn"}
    if command == "npm" and len(args) >= 3 and args[1] in {"run", "run-script"}:
        return args[2] in {"dev", "preview", "serve", "start"}
    if command == "npm" and len(args) >= 2:
        return args[1] in {"dev", "preview", "serve", "start"}
    return False


def python_module(args: list[str]) -> tuple[str, int] | None:
    if not args or not executable_name(args[0]).startswith("python"):
        return None
    for index, token in enumerate(args[1:], start=1):
        if token == "-m" and index + 1 < len(args):
            return args[index + 1], index + 1
        if token == "--":
            break
    return None


def non_loopback_bind_requested(args: list[str], assignments: dict[str, str]) -> bool:
    if not is_server_command(args):
        return False

    bind_variables = {"BIND", "FLOWSIGHT_HOST", "FLOW_SIGHT_HOST", "HOST", "UVICORN_HOST"}
    for name, value in assignments.items():
        if name.upper() in bind_variables and (
            not is_loopback_bind_value(value) or is_dynamic_shell_value(value)
        ):
            return True

    module = python_module(args)
    is_http_server = module is not None and module[0] == "http.server"
    http_bind: str | None = None

    for index, token in enumerate(args):
        if token in {"--host", "--bind", "-b"}:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                return True
            value = args[index + 1]
            if not is_loopback_bind_value(value) or is_dynamic_shell_value(value):
                return True
            if token in {"--bind", "-b"}:
                http_bind = value
        if token.startswith(("--host=", "--bind=")):
            value = token.split("=", 1)[1]
            if not is_loopback_bind_value(value) or is_dynamic_shell_value(value):
                return True
            if token.startswith("--bind="):
                http_bind = value
        if token.startswith("-b") and token != "-b":
            value = token[2:]
            if not is_loopback_bind_value(value) or is_dynamic_shell_value(value):
                return True
            http_bind = value
    if is_http_server:
        return http_bind != "127.0.0.1"
    return False


def find_mutates_files(args: list[str]) -> bool:
    if not args or executable_name(args[0]) != "find":
        return False
    return any(token in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for token in args)


def contains_process_substitution(command: str) -> bool:
    """Return true for unquoted process substitutions such as `<(cmd)`."""

    quote: str | None = None
    index = 0
    while index < len(command) - 1:
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            index += 2
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if command[index : index + 2] in {"<(", ">("}:
            return True
        index += 1
    return False


def shell_is_nonexecuting_check(args: list[str]) -> bool:
    if not args or executable_name(args[0]) not in SHELL_EXECUTABLES:
        return False
    return any(
        token == "--noexec"
        or (token.startswith("-") and not token.startswith("--") and "n" in token[1:])
        for token in args[1:]
    )


def inspect_command(command: str, cwd: Path, depth: int = 0) -> None:
    if depth > 4:
        deny("error: nested shell command depth exceeds the guard's reviewable limit.")

    # Shells remove an unquoted backslash-newline before parsing tokens.
    command = re.sub(r"\\\r?\n", "", command)
    if contains_process_substitution(command):
        deny("error: process substitution executes opaque nested commands and is blocked for agents.")
    command, substitutions = extract_command_substitutions(command)
    for substitution in substitutions:
        inspect_command(substitution, cwd, depth + 1)

    for segment in split_shell_segments(command):
        segment_args = strip_control_prefixes(argv(segment))
        if not segment_args:
            continue
        normalized, assignments = normalized_command(segment_args)
        if not normalized:
            continue

        if is_dynamic_shell_value(normalized[0]):
            deny("error: a dynamically computed executable cannot be reviewed safely.")

        if executable_name(normalized[0]) == "sudo":
            deny("error: privilege escalation with 'sudo' is blocked for FlowSight agents.")

        if executable_name(normalized[0]) in {"builtin", "enable", "eval", "source", "."}:
            deny("error: eval/source commands are opaque to the command guard and are blocked for agents.")

        if executable_name(normalized[0]) == "xargs":
            deny("error: xargs constructs commands from opaque stdin and is blocked for agents.")

        nested_script = shell_script(normalized)
        if nested_script is not None:
            inspect_command(nested_script, cwd, depth + 1)
        elif (
            executable_name(normalized[0]) in SHELL_EXECUTABLES
            and not shell_is_nonexecuting_check(normalized)
        ):
            deny("error: shell execution without an inspectable -c command is blocked for agents.")

        git_parts = git_subcommand_args(normalized, cwd)
        if git_parts:
            subcommand, rest, git_cwd, config_overrides, config_env_keys = git_parts
            if git_environment_changes_config(assignments) or any(
                dangerous_git_config_override(config) for config in config_overrides
            ) or any(
                dangerous_git_config_override(key) for key in config_env_keys
            ):
                deny("error: Git config overrides may not replace hooks or define/include aliases.")
            if any(is_dynamic_shell_value(token) for token in normalized):
                deny("error: dynamically constructed Git commands cannot be reviewed safely.")
            if configured_git_alias(subcommand, git_cwd):
                deny("error: Git aliases are opaque to the command guard; spell out the reviewed Git command.")
            if subcommand == "config" and git_config_changes_protected_key(rest):
                deny("error: changing Git hooks, aliases, or includes is blocked for agents.")
            if subcommand == "config" and git_config_write_forces_push(rest):
                deny("error: Git config may not enable forced remote updates for agents.")
            if subcommand == "reset" and any(
                long_option_matches(token, "--hard") for token in rest
            ):
                deny("error: 'git reset --hard' is blocked for agents. Ask the user before destructive git operations.")
            if subcommand == "push" and push_forces_remote_update(rest):
                deny("error: forced Git pushes are blocked for agents because they can rewrite remote refs.")
            if subcommand == "push" and any(
                git_config_override_forces_push(config) for config in config_overrides
            ):
                deny("error: Git config overrides may not enable forced remote updates for agents.")
            if subcommand == "push" and any(
                git_config_env_may_force_push(key) for key in config_env_keys
            ):
                deny("error: opaque Git config environment values may not control forced remote updates for agents.")
            if subcommand == "clean":
                deny("error: 'git clean' is blocked for agents. Ask the user before destructive git operations.")
            if subcommand == "stash":
                deny("error: 'git stash' is blocked for agents. Ask the user before destructive git operations.")
            if subcommand == "commit" and commit_skips_hooks(rest):
                deny("error: bypassing repository hooks with 'git commit --no-verify/-n' is blocked for agents.")
            if subcommand == "checkout" and checkout_updates_paths(rest, git_cwd):
                deny("error: destructive 'git checkout' path/force operations are blocked; use 'git switch' for branches.")
            if subcommand == "restore" and restore_updates_worktree(rest):
                deny("error: destructive 'git restore' worktree operations are blocked for agents.")
            if subcommand == "switch" and switch_discards_changes(rest):
                deny("error: destructive 'git switch --discard-changes/-f' is blocked for agents.")
            if subcommand == "rm":
                deny("error: 'git rm' is blocked for agents; remove files through a reviewed edit and stage normally.")
            if subcommand == "checkout-index" and any(
                short_option_contains(token, "f")
                or long_option_matches(token, "--force")
                for token in rest
            ):
                deny("error: forced 'git checkout-index' worktree replacement is blocked for agents.")
            if subcommand == "read-tree" and any(
                long_option_matches(token, "--reset") for token in rest
            ) and any(
                short_option_contains(token, "u")
                or long_option_matches(token, "--update")
                for token in rest
            ):
                deny("error: 'git read-tree --reset -u' worktree replacement is blocked for agents.")

        if rm_is_dangerous(normalized, cwd):
            deny("error: broad rm -rf command is blocked for agents.")

        if find_mutates_files(normalized):
            deny("error: mutating find actions are blocked for agents. Use a reviewed, narrow edit instead.")

        if executable_name(normalized[0]) == "chmod" and "-R" in normalized and "777" in normalized:
            deny("error: chmod -R 777 is blocked.")

        if non_loopback_bind_requested(normalized, assignments):
            deny("error: FlowSight v1 local servers must bind 127.0.0.1, not a public interface.")


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

    cwd = Path(payload.get("cwd") or Path.cwd()).resolve(strict=False)
    inspect_command(command, cwd)

    if "sys.settrace" in command and "global" in command.lower():
        deny("error: FlowSight v1 must not enable global sys.settrace.")


if __name__ == "__main__":
    main()
