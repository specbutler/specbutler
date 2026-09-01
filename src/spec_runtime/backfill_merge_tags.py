#!/usr/bin/env python3
"""Audit, backfill, and repair spec/merged/* tags."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from spec_runtime.config import load_repo_spec_runtime_config, resolve_specs_root
from spec_runtime.git_common import subprocess_text_kwargs
from spec_runtime.spec_merge_tags import (
    TAG_PREFIX,
    MergeTagProvenance,
    annotated_tag_command,
    build_tag_message,
    merge_tag_name,
    parse_tag_message,
    push_tag_command,
    shellify,
    spec_id_referenced,
    utc_timestamp_now,
)
from spec_runtime.spec_status import collect_git_spec_state


def _run_command(
    command: list[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        **subprocess_text_kwargs(command),
    )


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run_command(["git", *args], cwd=cwd)


def _run_gh(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run_command(["gh", *args], cwd=cwd)


def _git_lines(*args: str, cwd: Path) -> list[str]:
    result = _run_git(*args, cwd=cwd)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_stdout(*args: str, cwd: Path) -> str:
    result = _run_git(*args, cwd=cwd)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _current_actor() -> str:
    return (
        os.getenv("SPEC_ACTOR")
        or os.getenv("USER")
        or os.getenv("LOGNAME")
        or os.getenv("USERNAME")
        or "unknown"
    )


def _short_sha(value: str) -> str:
    return value[:12] if value else "unknown"


def _parse_frontmatter(spec_path: Path) -> dict[str, str]:
    text = spec_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    frontmatter: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^(\w[\w-]*):\s*(.*)", line)
        if match:
            frontmatter[match.group(1)] = match.group(2).strip()
    return frontmatter


def _frontmatter_merged_specs(repo_root: Path, *, specs_root: Path) -> list[str]:
    specs: list[str] = []
    for spec_path in sorted(specs_root.glob("*.md")):
        fm = _parse_frontmatter(spec_path)
        spec_id = fm.get("id", "").strip()
        if not spec_id or spec_id == "<slug>":
            continue
        if fm.get("status", "").strip() == "merged":
            specs.append(spec_id)
    return specs


def _all_merge_tag_spec_ids(repo_root: Path) -> set[str]:
    tags = _git_lines("tag", "--list", f"{TAG_PREFIX}*", cwd=repo_root)
    return {tag[len(TAG_PREFIX) :] for tag in tags if tag.startswith(TAG_PREFIX)}


def find_missing_merge_tags(repo_root: Path) -> tuple[list[str], list[str]]:
    """Return (missing_tag_specs, unreachable_tag_specs) for status: merged specs."""
    repo_config = load_repo_spec_runtime_config(repo_root)
    merged_specs = _frontmatter_merged_specs(
        repo_root,
        specs_root=resolve_specs_root(repo_root, config=repo_config),
    )
    reachable = collect_git_spec_state(repo_root, config=repo_config).merged_specs
    existing_tag_ids = _all_merge_tag_spec_ids(repo_root)

    missing: list[str] = []
    unreachable: list[str] = []
    for spec_id in merged_specs:
        if spec_id in reachable:
            continue
        if spec_id in existing_tag_ids:
            unreachable.append(spec_id)
            continue
        missing.append(spec_id)
    return missing, unreachable


def _resolve_ref_sha(repo_root: Path, ref: str) -> str:
    return _git_stdout("rev-parse", ref, cwd=repo_root)


def _tag_object_type(repo_root: Path, tag_name: str) -> str:
    return _git_stdout("cat-file", "-t", f"refs/tags/{tag_name}", cwd=repo_root)


def _tag_target_commit_sha(repo_root: Path, tag_name: str) -> str:
    return _resolve_ref_sha(repo_root, f"refs/tags/{tag_name}^{{commit}}")


def _tag_message(repo_root: Path, tag_name: str) -> str:
    raw = _git_stdout("cat-file", "-p", f"refs/tags/{tag_name}", cwd=repo_root)
    _header, sep, message = raw.partition("\n\n")
    return message if sep else ""


def _commit_reachable_from_base_ref(repo_root: Path, commit_sha: str, *, base_ref: str) -> bool:
    result = _run_git("merge-base", "--is-ancestor", commit_sha, base_ref, cwd=repo_root)
    return result.returncode == 0


def _commit_message(repo_root: Path, commit_sha: str) -> str:
    return _git_stdout("show", "-s", "--format=%B", commit_sha, cwd=repo_root)


def _commit_references_spec(repo_root: Path, commit_sha: str, spec_id: str) -> bool:
    return spec_id_referenced(_commit_message(repo_root, commit_sha), spec_id)


def _base_ref_commit_matches(repo_root: Path, spec_id: str, *, base_ref: str) -> list[str]:
    raw = _git_stdout("log", base_ref, "--format=%H%x00%B%x00", cwd=repo_root)
    if not raw:
        return []

    parts = raw.split("\x00")
    matches: list[str] = []
    for index in range(0, len(parts) - 1, 2):
        commit_sha = parts[index].strip()
        message = parts[index + 1]
        if commit_sha and spec_id_referenced(message, spec_id):
            matches.append(commit_sha)
    return matches


def _infer_commit_from_history(repo_root: Path, spec_id: str, *, base_ref: str) -> tuple[str, str]:
    matches = _base_ref_commit_matches(repo_root, spec_id, base_ref=base_ref)
    if len(matches) == 1:
        return matches[0], f"unique {base_ref} commit message match"
    if not matches:
        return "", f"no {base_ref} commit message references this spec id"
    return "", f"multiple {base_ref} commits reference this spec id"


def _format_push_command(tag_names: list[str], *, force: bool = False) -> str:
    refs = " ".join(f"{'+' if force else ''}refs/tags/{tag_name}" for tag_name in tag_names)
    return f"git push origin {refs}"


def _load_pr_metadata(
    repo_root: Path,
    pr_number: int,
    cache: dict[int, dict | None],
) -> dict | None:
    if pr_number in cache:
        return cache[pr_number]

    result = _run_gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "number,title,body,headRefName,mergeCommit,mergedAt,mergedBy",
        cwd=repo_root,
    )
    if result.returncode != 0:
        cache[pr_number] = None
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        cache[pr_number] = None
        return None
    cache[pr_number] = payload if isinstance(payload, dict) else None
    return cache[pr_number]


def _pr_merge_commit_sha(pr_data: dict | None) -> str:
    if not isinstance(pr_data, dict):
        return ""
    merge_commit = pr_data.get("mergeCommit")
    if not isinstance(merge_commit, dict):
        return ""
    return str(merge_commit.get("oid", "") or "").strip()


def _pr_references_spec(pr_data: dict | None, spec_id: str, commit_sha: str) -> bool:
    if not isinstance(pr_data, dict):
        return False

    merge_commit_sha = _pr_merge_commit_sha(pr_data)
    if not merge_commit_sha or merge_commit_sha != commit_sha:
        return False

    fragments = [
        str(pr_data.get("headRefName", "") or ""),
        str(pr_data.get("title", "") or ""),
        str(pr_data.get("body", "") or ""),
    ]
    return spec_id_referenced("\n".join(fragments), spec_id)


@dataclass
class TagAuditRecord:
    tag_name: str
    spec_id: str
    object_type: str = ""
    target_commit_sha: str = ""
    metadata: MergeTagProvenance | None = None
    commit_provenance_match: bool = False
    pr_provenance_match: bool = False
    issues: list[str] = field(default_factory=list)
    repair_target_sha: str = ""
    repair_provenance: MergeTagProvenance | None = None

    @property
    def is_suspicious(self) -> bool:
        return bool(self.issues)


def _build_repair_provenance(
    record: TagAuditRecord,
    *,
    target_sha: str,
    pr_data: dict | None,
) -> MergeTagProvenance:
    existing = record.metadata
    merged_by = ""
    merged_at = ""
    if isinstance(pr_data, dict):
        merged_by_data = pr_data.get("mergedBy")
        if isinstance(merged_by_data, dict):
            merged_by = str(merged_by_data.get("login", "") or "").strip()
        merged_at = str(pr_data.get("mergedAt", "") or "").strip()

    source_branch = ""
    if existing and existing.source_branch:
        source_branch = existing.source_branch
    elif isinstance(pr_data, dict):
        source_branch = str(pr_data.get("headRefName", "") or "").strip()
    if not source_branch:
        source_branch = "manual-repair"

    actor = existing.actor if existing and existing.actor else merged_by or _current_actor()
    timestamp = existing.timestamp if existing and existing.timestamp else merged_at or utc_timestamp_now()

    pr_number = None
    if existing and existing.pr_number is not None:
        pr_number = existing.pr_number
    elif isinstance(pr_data, dict) and isinstance(pr_data.get("number"), int):
        pr_number = int(pr_data["number"])

    return MergeTagProvenance(
        spec_id=record.spec_id,
        merge_commit_sha=target_sha,
        pr_number=pr_number,
        source_branch=source_branch,
        actor=actor,
        timestamp=timestamp,
    )


def _assign_repair_plan(
    record: TagAuditRecord,
    *,
    base_ref: str,
    repo_root: Path,
    pr_cache: dict[int, dict | None],
) -> None:
    pr_data = None
    if record.metadata and record.metadata.pr_number is not None:
        pr_data = _load_pr_metadata(repo_root, record.metadata.pr_number, pr_cache)

    target_sha = ""
    if record.commit_provenance_match or record.pr_provenance_match:
        target_sha = record.target_commit_sha
    else:
        pr_merge_sha = _pr_merge_commit_sha(pr_data)
        if pr_merge_sha and _pr_references_spec(pr_data, record.spec_id, pr_merge_sha):
            target_sha = pr_merge_sha
        else:
            target_sha, _reason = _infer_commit_from_history(
                repo_root,
                record.spec_id,
                base_ref=base_ref,
            )

    if not target_sha:
        return

    record.repair_target_sha = target_sha
    record.repair_provenance = _build_repair_provenance(
        record,
        target_sha=target_sha,
        pr_data=pr_data,
    )


def _audit_existing_tags(repo_root: Path) -> list[TagAuditRecord]:
    repo_config = load_repo_spec_runtime_config(repo_root)
    base_ref = repo_config.base_ref
    tags = sorted(_git_lines("tag", "--list", f"{TAG_PREFIX}*", cwd=repo_root))
    pr_cache: dict[int, dict | None] = {}
    records: list[TagAuditRecord] = []

    for tag_name in tags:
        spec_id = tag_name[len(TAG_PREFIX) :]
        record = TagAuditRecord(tag_name=tag_name, spec_id=spec_id)
        record.object_type = _tag_object_type(repo_root, tag_name)
        if record.object_type != "tag":
            record.issues.append("tag is lightweight; expected an annotated tag object")

        record.target_commit_sha = _tag_target_commit_sha(repo_root, tag_name)
        if not record.target_commit_sha:
            record.issues.append("tag does not resolve to a commit")
            records.append(record)
            continue

        if not _commit_reachable_from_base_ref(repo_root, record.target_commit_sha, base_ref=base_ref):
            record.issues.append(f"tagged commit is not reachable from {base_ref}")

        if record.object_type == "tag":
            metadata, error = parse_tag_message(_tag_message(repo_root, tag_name))
            if error:
                record.issues.append(f"tag metadata invalid: {error}")
            else:
                record.metadata = metadata
                if metadata.spec_id != spec_id:
                    record.issues.append(f"tag metadata spec_id={metadata.spec_id!r} does not match ref name")
                if metadata.merge_commit_sha != record.target_commit_sha:
                    record.issues.append("tag metadata merge_commit_sha does not match the tagged commit")

        record.commit_provenance_match = _commit_references_spec(
            repo_root,
            record.target_commit_sha,
            spec_id,
        )
        pr_data = None
        if record.metadata and record.metadata.pr_number is not None:
            pr_data = _load_pr_metadata(repo_root, record.metadata.pr_number, pr_cache)
        record.pr_provenance_match = _pr_references_spec(
            pr_data,
            spec_id,
            record.target_commit_sha,
        )
        if not (record.commit_provenance_match or record.pr_provenance_match):
            record.issues.append("tagged commit and associated PR provenance do not reference the spec id")

        records.append(record)

    records_by_commit: dict[str, list[TagAuditRecord]] = {}
    for record in records:
        if record.target_commit_sha:
            records_by_commit.setdefault(record.target_commit_sha, []).append(record)

    for commit_sha, grouped_records in records_by_commit.items():
        if len(grouped_records) < 2:
            continue
        unresolved = [
            record for record in grouped_records if not (record.commit_provenance_match or record.pr_provenance_match)
        ]
        if not unresolved:
            continue
        grouped_names = ", ".join(record.tag_name for record in grouped_records)
        for record in unresolved:
            record.issues.append(
                f"suspicious many-to-one mapping: {grouped_names} all point at {_short_sha(commit_sha)}"
            )

    for record in records:
        if record.is_suspicious:
            _assign_repair_plan(
                record,
                base_ref=base_ref,
                repo_root=repo_root,
                pr_cache=pr_cache,
            )

    return records


def _print_tag_audit(records: list[TagAuditRecord]) -> None:
    if not records:
        return
    print("Suspicious merge tags:")
    for record in records:
        target = record.target_commit_sha or "unknown"
        print(f"- {record.tag_name} -> {target}")
        for issue in record.issues:
            print(f"  - {issue}")


def cmd_audit(repo_root: Path) -> int:
    base_ref = load_repo_spec_runtime_config(repo_root).base_ref
    missing, unreachable = find_missing_merge_tags(repo_root)
    suspicious = [record for record in _audit_existing_tags(repo_root) if record.is_suspicious]
    if not missing and not unreachable and not suspicious:
        print("OK: reachable spec/merged/<id> tags are present, annotated, and their provenance matches the spec ids.")
        return 0

    if missing:
        print("Missing merge tags for frontmatter status: merged specs:")
        for spec_id in missing:
            print(f"- {spec_id}")

    if unreachable:
        print(f"Merge tags exist but are not reachable from {base_ref}:")
        for spec_id in unreachable:
            print(f"- {spec_id}")
        print("Backfill will not rewrite existing tags; retag these specs manually.")

    _print_tag_audit(suspicious)

    if missing:
        print("Run backfill to create missing tags:")
        print("spec merge-tags-backfill --dry-run")
    if suspicious:
        print("Run repair to inspect suspicious tags:")
        print("spec merge-tags-repair")
    return 1


def _backfill_provenance(spec_id: str, target_sha: str) -> MergeTagProvenance:
    return MergeTagProvenance(
        spec_id=spec_id,
        merge_commit_sha=target_sha,
        pr_number=None,
        source_branch="historical-backfill",
        actor=_current_actor(),
        timestamp=utc_timestamp_now(),
    )


def cmd_backfill(repo_root: Path, *, ref: str, dry_run: bool, push: bool) -> int:
    base_ref = load_repo_spec_runtime_config(repo_root).base_ref
    missing, unreachable = find_missing_merge_tags(repo_root)
    if unreachable:
        print(f"Found merge tags that are not reachable from {base_ref} (left unchanged):")
        for spec_id in unreachable:
            print(f"- {spec_id}")
        print("Resolve these manually, then rerun backfill/audit.")

    if not missing:
        print("No missing merge tags to create.")
        return 1 if unreachable else 0

    created_tags: list[str] = []
    unresolved_specs: list[str] = []
    explicit_ref_sha = _resolve_ref_sha(repo_root, ref) if ref else ""
    if ref and not explicit_ref_sha:
        print(f"ERROR: could not resolve ref {ref!r}", file=sys.stderr)
        return 1

    for spec_id in missing:
        tag_name = merge_tag_name(spec_id)
        target_sha = explicit_ref_sha
        reason = ""
        if not target_sha:
            target_sha, reason = _infer_commit_from_history(repo_root, spec_id, base_ref=base_ref)
        if not target_sha:
            unresolved_specs.append(spec_id)
            print(
                f"Could not infer a unique merge commit for {spec_id}: {reason}. "
                f"Inspect history, then rerun with --ref <commit>."
            )
            continue

        provenance = _backfill_provenance(spec_id, target_sha)
        command = annotated_tag_command(
            tag_name,
            target_sha,
            build_tag_message(provenance),
        )
        if dry_run:
            print(f"DRY-RUN: {shellify(command)}")
            created_tags.append(tag_name)
            continue

        result = _run_command(command, cwd=repo_root)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            print(f"ERROR: failed to create {tag_name}: {detail}", file=sys.stderr)
            return 1

        created_tags.append(tag_name)
        origin_note = f" ({reason})" if reason else ""
        print(f"Created annotated tag {tag_name} -> {target_sha}{origin_note}")

    if dry_run and created_tags:
        print(f"Would create {len(created_tags)} tags.")
        print("After creating tags, push them so all clones/CI resolve merged status:")
        print(_format_push_command(created_tags))

    if not dry_run and created_tags:
        if push:
            push_result = _run_git(
                "push",
                "origin",
                *[f"refs/tags/{tag_name}" for tag_name in created_tags],
                cwd=repo_root,
            )
            if push_result.returncode != 0:
                detail = push_result.stderr.strip() or push_result.stdout.strip() or "unknown error"
                print(f"ERROR: failed to push merge tags: {detail}", file=sys.stderr)
                return 1
            print(f"Pushed {len(created_tags)} tags to origin.")
        else:
            print("Push tags so all clones/CI resolve merged status:")
            print(_format_push_command(created_tags))

    return 1 if unreachable or unresolved_specs else 0


def cmd_repair(repo_root: Path, *, force: bool) -> int:
    suspicious = [record for record in _audit_existing_tags(repo_root) if record.is_suspicious]
    if not suspicious:
        print("No suspicious merge tags to repair.")
        return 0

    repairable = [record for record in suspicious if record.repair_target_sha and record.repair_provenance is not None]
    unresolved = [record for record in suspicious if record not in repairable]

    _print_tag_audit(suspicious)

    if repairable:
        print("Repair plan:")
        for record in repairable:
            movement = record.repair_target_sha
            if movement != record.target_commit_sha:
                movement = f"{_short_sha(record.target_commit_sha)} -> {_short_sha(record.repair_target_sha)}"
            else:
                movement = f"re-annotate {_short_sha(record.repair_target_sha)}"
            print(f"- {record.tag_name}: {movement}")
            tag_command = annotated_tag_command(
                record.tag_name,
                record.repair_target_sha,
                build_tag_message(record.repair_provenance),
                force=True,
            )
            push_command = push_tag_command(record.tag_name, force=True)
            if force:
                tag_result = _run_command(tag_command, cwd=repo_root)
                if tag_result.returncode != 0:
                    detail = tag_result.stderr.strip() or tag_result.stdout.strip()
                    print(
                        f"ERROR: failed to rewrite {record.tag_name}: {detail}",
                        file=sys.stderr,
                    )
                    return 1
                push_result = _run_command(push_command, cwd=repo_root)
                if push_result.returncode != 0:
                    detail = push_result.stderr.strip() or push_result.stdout.strip()
                    print(
                        f"ERROR: failed to push {record.tag_name}: {detail}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"Repaired {record.tag_name}")
            else:
                print(f"  DRY-RUN: {shellify(tag_command)}")
                print(f"  DRY-RUN: {shellify(push_command)}")

    if unresolved:
        print("Manual inspection still required:")
        for record in unresolved:
            print(f"- {record.tag_name}")
            print(f"  Inspect with: git cat-file -p refs/tags/{record.tag_name}")
            if record.target_commit_sha:
                print(f"  Commit history: git show --stat --summary {record.target_commit_sha}")

    if force:
        return 1 if unresolved else 0

    print("Pass --force to rewrite the repairable tags above.")
    return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit/backfill spec merge tags.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_audit = sub.add_parser("audit")
    p_audit.add_argument("--repo-root", default=".")

    p_backfill = sub.add_parser("backfill")
    p_backfill.add_argument("--repo-root", default=".")
    p_backfill.add_argument(
        "--ref",
        default="",
        help=(
            "Explicit commit/ref to tag. When omitted, backfill infers a unique "
            "configured base-ref commit whose message references the spec id."
        ),
    )
    p_backfill.add_argument("--dry-run", action="store_true")
    p_backfill.add_argument(
        "--push",
        action="store_true",
        help="Push created tags to origin after local creation.",
    )

    p_repair = sub.add_parser("repair")
    p_repair.add_argument("--repo-root", default=".")
    p_repair.add_argument(
        "--force",
        action="store_true",
        help="Rewrite and force-push repairable tags instead of printing a dry-run plan.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    repo_root = Path(args.repo_root).resolve()
    if args.cmd == "audit":
        return cmd_audit(repo_root)
    if args.cmd == "backfill":
        return cmd_backfill(
            repo_root,
            ref=args.ref,
            dry_run=args.dry_run,
            push=args.push,
        )
    if args.cmd == "repair":
        return cmd_repair(
            repo_root,
            force=args.force,
        )
    raise RuntimeError(f"Unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
