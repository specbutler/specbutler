"""Bound the host-materialized diff a local reviewer gets in its prompt.

The reviewer's evidence is the exact diff the host computes. Pasting all of it
into the prompt has no upper bound: a pull request with a large generated file
pushed a Codex review prompt past the provider's input limit
(``input_too_large``). The host instead keeps the whole diff as per-file
evidence the reviewer reads on demand, and puts only a manifest plus the
hand-written files' diffs that fit a character budget in the prompt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .review_convergence import _diff_git_header_paths, _diff_side_path, _unquote_git_path

# Well under the smallest provider input limit (Codex: 1,048,576 characters),
# leaving room for the review contract, spec and gate evidence.
INLINE_DIFF_MAX_CHARS = 200_000

STATUS_INLINE = "inline"
STATUS_READ = "read_diff"
STATUS_GENERATED = "generated"


@dataclass(frozen=True)
class DiffFile:
    path: str
    text: str
    added: int
    removed: int


@dataclass(frozen=True)
class DiffPlan:
    files: tuple[DiffFile, ...]
    statuses: dict[str, str]
    total_chars: int

    @property
    def complete(self) -> bool:
        """True when every file's diff is in the prompt."""
        return all(status == STATUS_INLINE for status in self.statuses.values())

    def inline_text(self) -> str:
        return "".join(f.text for f in self.files if self.statuses[f.path] == STATUS_INLINE)


def _section_path(lines: list[str]) -> str:
    old = new = None
    for line in lines[1:]:
        if line.startswith("@@"):
            break
        if line.startswith("--- "):
            old = _diff_side_path(line[4:], "a/")
        elif line.startswith("+++ "):
            new = _diff_side_path(line[4:], "b/")
        elif line.startswith(("rename to ", "copy to ")):
            new = _unquote_git_path(line.split(" to ", 1)[1])
        elif line.startswith(("rename from ", "copy from ")):
            old = _unquote_git_path(line.split(" from ", 1)[1])
    if new or old:
        return new or old or ""
    header = _diff_git_header_paths(lines[0]) if lines else []
    if header:
        return header[0]
    return lines[0][len("diff --git "):] if lines else ""


def split_diff_by_file(diff_text: str) -> list[DiffFile]:
    """Split ``git diff`` output into exact per-file sections, in diff order.

    Concatenating every section's text reproduces ``diff_text``. Text before
    the first ``diff --git`` header (normally none) stays with the first file.
    """
    sections: list[list[str]] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") or not sections:
            sections.append([])
        sections[-1].append(line)
    files: list[DiffFile] = []
    seen: dict[str, int] = {}
    for lines in sections:
        stripped = [line.rstrip("\r\n") for line in lines]
        header_index = next((i for i, line in enumerate(stripped) if line.startswith("diff --git ")), 0)
        path = _section_path(stripped[header_index:]) or "(diff)"
        if path in seen:
            seen[path] += 1
            path = f"{path} ({seen[path]})"
        else:
            seen[path] = 1
        added = removed = 0
        in_hunk = False
        for line in stripped:
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                added += 1
            elif in_hunk and line.startswith("-"):
                removed += 1
        files.append(DiffFile(path=path, text="".join(lines), added=added, removed=removed))
    return files


def plan_review_diff(
    diff_text: str,
    *,
    generated: frozenset[str] | set[str] = frozenset(),
    budget: int = INLINE_DIFF_MAX_CHARS,
) -> DiffPlan:
    """Choose which files' diffs go in the prompt.

    A diff within budget with no generated files goes in whole, exactly as
    before. Otherwise generated files are never inlined, and hand-written
    files are inlined in diff order while they fit; the rest are read on
    demand.
    """
    files = tuple(split_diff_by_file(diff_text))
    statuses: dict[str, str] = {}
    is_generated = {f.path: f.path in generated for f in files}
    if len(diff_text) <= budget and not any(is_generated.values()):
        statuses = {f.path: STATUS_INLINE for f in files}
    else:
        used = 0
        for f in files:
            if is_generated[f.path]:
                statuses[f.path] = STATUS_GENERATED
            elif used + len(f.text) <= budget:
                statuses[f.path] = STATUS_INLINE
                used += len(f.text)
            else:
                statuses[f.path] = STATUS_READ
    return DiffPlan(files=files, statuses=statuses, total_chars=len(diff_text))


def render_manifest(plan: DiffPlan) -> str:
    rows = ["status\tadded\tremoved\tchars\tpath"]
    rows += [
        f"{plan.statuses[f.path]}\t+{f.added}\t-{f.removed}\t{len(f.text)}\t{f.path}"
        for f in plan.files
    ]
    return "\n".join(rows) + "\n"


def write_evidence(path: Path, plan: DiffPlan, *, base_sha: str, head_sha: str) -> None:
    """Write the per-file diffs the review inspection server serves."""
    payload = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "manifest": render_manifest(plan),
        "files": {f.path: f.text for f in plan.files},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def load_evidence(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("manifest"), str)
        or not isinstance(payload.get("files"), dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in payload["files"].items())
    ):
        raise ValueError("Malformed review diff evidence")
    return payload


def summarize_oversized_diff(diff_text: str, *, limit: int = 40) -> str:
    """One line per changed file, for a diff too large to include."""
    files = split_diff_by_file(diff_text)
    lines = [f"  +{f.added} -{f.removed}\t{f.path}" for f in files[:limit]]
    if len(files) > limit:
        lines.append(f"  … and {len(files) - limit} more files")
    return "\n".join(lines) + "\n"
