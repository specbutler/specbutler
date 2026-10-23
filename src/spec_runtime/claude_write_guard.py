"""Claude Code PreToolUse hook that keeps file-editing tools inside the workspace.

Claude's OS sandbox confines Bash, but Edit, Write, MultiEdit and NotebookEdit
run in the Claude process itself, and bypassPermissions mode skips their
protected-path prompts. Spec Butler registers this script for those tools in
the generated settings of native unattended launches.

Hooks run whatever the permission mode, so the script also refuses the Bash
commands the settings' deny rules name (``git reset --hard`` and force
pushes) rather than relying on those rules surviving bypass mode.

It runs as ``python -I claude_write_guard.py --allow ROOT ... --deny PATH ...``
and must import nothing outside the standard library. It reads the hook
payload from stdin and exits 2 (block, message on stderr) unless the resolved
target is inside an ``--allow`` root and outside every ``--deny`` path. Any
malformed input is blocked.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys

GUARDED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
BLOCK = 2
_RESET_REASON = "`git reset --hard` discards work; commit or stash instead"
_PUSH_REASON = "force pushes are not allowed; the orchestrator publishes the branch"
# Git global options whose value is the next argument.
_GIT_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
     "--config-env", "--exec-path", "--list-cmds"}
)
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", "(", ")", "{", "}"})
_FORCE_LONG = ("--force", "--force-with-lease", "--force-if-includes")
# Used only when the command cannot be tokenized (unbalanced quotes).
_FALLBACK_RE = re.compile(
    r"\bgit\b.*?(?:\breset\b.*?--hard\b|\bpush\b.*?(?:--force|\s-[A-Za-z]*f|\s\+\S))"
)
_MAX_NESTING = 4


def _block(message: str) -> int:
    print(f"Spec Butler write guard: {message}", file=sys.stderr)
    return BLOCK


def _parse_args(argv: list[str]) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    denied: list[str] = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag not in {"--allow", "--deny"} or index + 1 >= len(argv):
            raise ValueError(f"unexpected argument {flag!r}")
        target = allowed if flag == "--allow" else denied
        target.append(os.path.realpath(argv[index + 1]))
        index += 2
    return allowed, denied


def _within(path: str, root: str, *, fold_case: bool) -> bool:
    if fold_case:
        path, root = path.casefold(), root.casefold()
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def check(payload: object, allowed: list[str], denied: list[str]) -> str:
    """Return an empty string to allow the call, else the block reason."""
    if not isinstance(payload, dict):
        return "hook input is not a JSON object"
    tool = payload.get("tool_name")
    if tool == "Bash":
        return _check_command(payload.get("tool_input"))
    if tool not in GUARDED_TOOLS:
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return f"{tool} call has no tool_input"
    raw = tool_input.get("notebook_path" if tool == "NotebookEdit" else "file_path")
    if not isinstance(raw, str) or not raw.strip():
        return f"{tool} call has no target path"
    cwd = payload.get("cwd")
    base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd()
    # realpath follows symlinks, so a link inside the workspace cannot carry
    # a write outside it.
    target = os.path.realpath(os.path.join(base, os.path.expanduser(raw)))
    # Case-insensitive volumes (the macOS default) let a differently cased
    # spelling reach a protected path, so deny matching folds case.
    for path in denied:
        if _within(target, path, fold_case=True):
            return (
                f"{tool} may not modify {target}: it is protected by the "
                "orchestrator. Change files in the workspace instead."
            )
    if not any(_within(target, root, fold_case=False) for root in allowed):
        roots = ", ".join(allowed) or "(none)"
        return (
            f"{tool} may not write {target}: it is outside the workspace. "
            f"Allowed locations: {roots}."
        )
    try:
        info = os.lstat(target)
    except OSError:
        return ""
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        return (
            f"{tool} may not modify {target}: it is a hard link and may share "
            "its contents with a file outside the workspace."
        )
    return ""


def _check_command(tool_input: object) -> str:
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return "Bash call has no command"
    reason = _denied_command_reason(command, depth=0)
    return f"refused `{command.strip()[:200]}`: {reason}." if reason else ""


def _simple_commands(command: str) -> list[list[str]]:
    """Split *command* into the argv of each simple command."""
    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    for token in lexer:
        if token in _SHELL_SEPARATORS or set(token) <= set(";&|()"):
            commands.append([])
        else:
            commands[-1].append(token)
    return [argv for argv in commands if argv]


def _denied_command_reason(command: str, *, depth: int) -> str:
    try:
        commands = _simple_commands(command)
    except ValueError:
        return _PUSH_REASON if _FALLBACK_RE.search(command) else ""
    for argv in commands:
        reason = _denied_git_reason(argv)
        if reason:
            return reason
        if depth < _MAX_NESTING:
            # ``sh -c '…'``, ``eval "…"`` and similar carry a nested script.
            for token in argv:
                if any(ch.isspace() for ch in token) or any(ch in token for ch in ";&|"):
                    reason = _denied_command_reason(token, depth=depth + 1)
                    if reason:
                        return reason
    return ""


def _denied_git_reason(argv: list[str]) -> str:
    for index, token in enumerate(argv):
        if os.path.basename(token) != "git":
            continue
        position = index + 1
        while position < len(argv) and argv[position].startswith("-"):
            option = argv[position]
            position += 2 if option in _GIT_VALUE_OPTIONS else 1
        if position >= len(argv):
            continue
        subcommand, args = argv[position], argv[position + 1 :]
        if subcommand == "reset" and "--hard" in args:
            return _RESET_REASON
        if subcommand == "push" and any(_is_force_push_arg(arg) for arg in args):
            return _PUSH_REASON
    return ""


def _is_force_push_arg(arg: str) -> bool:
    if arg.split("=", 1)[0] in _FORCE_LONG:
        return True
    if arg.startswith("+") and len(arg) > 1:
        return True
    return arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]


def main(argv: list[str] | None = None) -> int:
    try:
        allowed, denied = _parse_args(list(sys.argv[1:] if argv is None else argv))
    except ValueError as exc:
        return _block(f"misconfigured hook ({exc})")
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        return _block(f"could not read hook input ({exc})")
    reason = check(payload, allowed, denied)
    return _block(reason) if reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
