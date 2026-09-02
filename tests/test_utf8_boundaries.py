"""Repository and artifact text remains UTF-8 when locale UTF-8 mode is off."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repository_text_boundaries_ignore_legacy_process_locale(tmp_path: Path) -> None:
    # POSIX intentionally follows an ASCII filesystem encoding in this probe.
    # Non-ASCII path handling is covered by the native Windows probe below;
    # this hermetic test focuses on repository content and state artifacts.
    repo = tmp_path / "repo"
    specs = repo / "specs"
    specs.mkdir(parents=True)
    config_path = repo / ".spec.toml"
    config_path.write_text(
        '# Config snow 雪 and probe 🧪\nbase_ref = "HEAD"\n\n'
        '[paths]\nspecs_dir = "specs"\n',
        encoding="utf-8",
    )
    spec_path = specs / "unicode-content.md"
    original_spec = """---
id: unicode-content
description: Snow 雪 and probe 🧪
---

# Unicode content

## Acceptance Criteria

- [ ] Preserve snow 雪 and probe 🧪 exactly.
"""
    spec_path.write_text(original_spec, encoding="utf-8")
    (repo / "README.md").write_text("README snow 雪 and probe 🧪\n", encoding="utf-8")
    (repo / ".spec.local.toml").write_text(
        "# Local snow 雪 and probe 🧪\n",
        encoding="utf-8",
    )

    script = r"""
import json
import locale
import sys
from pathlib import Path

from spec_runtime.autopilot import run_log_alias_path, write_run_log_alias
from spec_runtime.autopilot_tui.app import _mark_spec_obsolete
from spec_runtime.config import load_spec_runtime_config
from spec_runtime.coordinator_bootstrap import _write_local_coordination
from spec_runtime.init import _gather_repo_context
from spec_runtime.orchestrator import (
    PINNED_SPEC_FILENAME,
    ReviewResult as OrchestratorReviewResult,
    RunState,
    _persist_pinned_spec,
)
from spec_runtime.review_feedback import ReviewResult as FeedbackReviewResult
from spec_runtime.review_gate import _write_text
from spec_runtime.spec_metadata import parse_spec_body, parse_spec_frontmatter

repo = Path(sys.argv[1])
config = load_spec_runtime_config(config_path=repo / ".spec.toml")
spec_path = repo / config.paths.specs_dir / "unicode-content.md"
spec_text = spec_path.read_bytes().decode("utf-8")
frontmatter = parse_spec_frontmatter(spec_path)
body = parse_spec_body(spec_path)
assert frontmatter["description"] == "Snow \u96ea and probe \U0001f9ea"
assert "Preserve snow \u96ea and probe \U0001f9ea exactly." in body
assert "README snow \u96ea and probe \U0001f9ea" in _gather_repo_context(repo)

run = RunState(run_id="unicode-run", spec_id="unicode-content", branch="code/unicode-content--run")
_persist_pinned_spec(
    repo,
    run,
    spec_path=f"{config.paths.specs_dir}/unicode-content.md",
    text=spec_text,
)
snapshot = repo / ".spec-state" / "runs" / run.run_id / PINNED_SPEC_FILENAME
assert snapshot.read_bytes() == spec_text.encode("utf-8")

_mark_spec_obsolete(spec_path)
mutated_spec = spec_path.read_bytes().decode("utf-8")
assert "obsolete: true" in mutated_spec
assert "Preserve snow \u96ea and probe \U0001f9ea exactly." in mutated_spec

unicode_log = str(repo / "logs-\u96ea" / "probe-\U0001f9ea.log")
write_run_log_alias(repo, run.run_id, unicode_log)
assert run_log_alias_path(repo, run.run_id).read_bytes().decode("utf-8").strip() == unicode_log

local_config = repo / ".spec.local.toml"
_write_local_coordination(local_config, {"url": "https://example.invalid/\u96ea"})
local_text = local_config.read_bytes().decode("utf-8")
assert "Local snow \u96ea and probe \U0001f9ea" in local_text
assert "https://example.invalid/\u96ea" in local_text

review_summary = repo / "review-summary.md"
_write_text(review_summary, "Review snow \u96ea and probe \U0001f9ea")
assert review_summary.read_bytes().decode("utf-8") == "Review snow \u96ea and probe \U0001f9ea\n"

review_result = repo / ".spec-state" / "runs" / "feedback-run" / "review-result.json"
review_result.parent.mkdir(parents=True, exist_ok=True)
review_result.write_bytes(json.dumps({
    "status": "approved",
    "summary": "Feedback snow \u96ea and probe \U0001f9ea",
}, ensure_ascii=False).encode("utf-8"))
assert FeedbackReviewResult.load(repo, "feedback-run").summary == (
    "Feedback snow \u96ea and probe \U0001f9ea"
)
assert OrchestratorReviewResult.load_from_path(review_result).summary == (
    "Feedback snow \u96ea and probe \U0001f9ea"
)

print(json.dumps({
    "default_encoding": locale.getencoding(),
    "utf8_mode": sys.flags.utf8_mode,
    "specs_dir": config.paths.specs_dir,
}))
"""
    env = os.environ.copy()
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONUTF8"] = "0"
    env["PYTHONCOERCECLOCALE"] = "0"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    if os.name != "nt":
        env["LC_ALL"] = "C"
        env["LANG"] = "C"

    completed = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", script, str(repo)],
        cwd=repo,
        env=env,
        capture_output=True,
        encoding="ascii",
        errors="strict",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["utf8_mode"] == 0
    assert result["specs_dir"] == "specs"
    assert result["default_encoding"].lower().replace("-", "") not in {
        "utf8",
        "utf8sig",
    }
