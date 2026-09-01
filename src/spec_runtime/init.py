"""``spec init`` — bootstrap a repository for spec-driven development."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from .git_common import run_git


def _git_repo_root() -> Path | None:
    """Return the git repo root, or None if not in a git repo."""
    try:
        result = run_git(
            ["rev-parse", "--show-toplevel"],
            check=True,
        )
        return Path(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def _has_remote(repo_root: Path, remote: str = "origin") -> bool:
    """Check if the named remote exists."""
    result = run_git(
        ["remote", "get-url", remote],
        cwd=repo_root,
    )
    return result.returncode == 0


def _detect_base_branch(repo_root: Path) -> str:
    """Detect the default branch name from the remote or local refs."""
    has_origin = _has_remote(repo_root)

    if has_origin:
        # Try remote HEAD
        try:
            result = run_git(
                ["remote", "show", "origin"],
                timeout=10,
                cwd=repo_root,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "HEAD branch:" in line:
                        branch = line.split("HEAD branch:")[-1].strip()
                        if branch and branch != "(unknown)":
                            return f"origin/{branch}"
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Fall back to checking local refs — only use origin/ prefix if remote exists
    for name in ("main", "master"):
        check = run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=repo_root,
        )
        if check.returncode == 0:
            if has_origin:
                return f"origin/{name}"
            return name

    return "origin/main" if has_origin else "main"


def _detect_agents() -> tuple[str, list[str]]:
    """Detect available agents on PATH. Returns (default, allowed)."""
    available = []
    for name in ("claude", "codex"):
        if shutil.which(name):
            available.append(name)

    if not available:
        return "", []

    default = available[0]
    return default, available


def _normalize_requirement_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _detect_python_tooling(repo_root: Path) -> tuple[bool, bool, frozenset[str] | None]:
    """Return detected verify tools and direct requirements in the dev extra."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return False, False, None

    try:
        raw = tomllib.loads(pyproject_path.read_text())
    except Exception:
        return False, False, None

    tools = raw.get("tool", {})
    pytest_config = tools.get("pytest", {})
    has_pytest = "pytest" in tools or (
        isinstance(pytest_config, dict) and "ini_options" in pytest_config
    )
    has_ruff = "ruff" in tools

    project = raw.get("project", {})
    optional_dependencies = (
        project.get("optional-dependencies", {}) if isinstance(project, dict) else {}
    )
    raw_dev = optional_dependencies.get("dev") if isinstance(optional_dependencies, dict) else None
    if not isinstance(raw_dev, list):
        return has_pytest, has_ruff, None

    dev_requirements: set[str] = set()
    for raw_requirement in raw_dev:
        if not isinstance(raw_requirement, str):
            continue
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            continue
        try:
            if requirement.marker is not None and not requirement.marker.evaluate(
                environment={"extra": "dev"}
            ):
                continue
        except Exception:
            # An unresolvable marker does not prove the tool will be installed.
            continue
        dev_requirements.add(_normalize_requirement_name(requirement.name))
    return has_pytest, has_ruff, frozenset(dev_requirements)


def _detect_install_command(repo_root: Path) -> str:
    """Auto-detect the command to install dependencies in a fresh worktree."""
    # Makefile with install target takes precedence (may wrap pip + npm)
    makefile_path = repo_root / "Makefile"
    if makefile_path.is_file():
        try:
            if re.search(r"^install\s*:", makefile_path.read_text(), re.MULTILINE):
                return "make install"
        except Exception:
            pass

    # Python project
    if (repo_root / "pyproject.toml").is_file() or (repo_root / "setup.py").is_file():
        has_pytest, has_ruff, dev_requirements = _detect_python_tooling(repo_root)
        install_target = "-e '.[dev]'" if dev_requirements is not None else "-e ."
        verify_tools = [
            tool
            for tool, detected in (("pytest", has_pytest), ("ruff", has_ruff))
            if detected and tool not in (dev_requirements or ())
        ]
        install_args = " ".join((install_target, *verify_tools))
        return (
            "python -m venv .venv && "
            f".venv/bin/python -m pip install {install_args}"
        )

    # Node project
    if (repo_root / "package.json").is_file():
        if (repo_root / "package-lock.json").is_file():
            return "npm ci"
        return "npm install"

    return ""


def _detect_verify_gates(repo_root: Path) -> list[dict]:
    """Auto-detect verify gates from project config files."""
    gates: list[dict] = []
    seen_names: set[str] = set()

    # Check pyproject.toml
    pyproject_path = repo_root / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            has_pytest, has_ruff, _has_dev_extra = _detect_python_tooling(repo_root)
            uses_project_venv = _detect_install_command(repo_root).startswith(
                "python -m venv .venv && "
            )
            if has_pytest:
                test_command = (
                    ".venv/bin/python -m pytest" if uses_project_venv else "pytest"
                )
                gate = {"name": "test", "command": test_command, "parallel": True}
                if uses_project_venv:
                    gate["argv_windows"] = [
                        ".venv/Scripts/python.exe",
                        "-m",
                        "pytest",
                    ]
                gates.append(gate)
                seen_names.add("test")
            if has_ruff:
                lint_command = (
                    ".venv/bin/python -m ruff check ."
                    if uses_project_venv
                    else "ruff check ."
                )
                gate = {"name": "lint", "command": lint_command, "parallel": True}
                if uses_project_venv:
                    gate["argv_windows"] = [
                        ".venv/Scripts/python.exe",
                        "-m",
                        "ruff",
                        "check",
                        ".",
                    ]
                gates.append(gate)
                seen_names.add("lint")
        except Exception:
            pass

    # Check Makefile
    makefile_path = repo_root / "Makefile"
    if makefile_path.is_file():
        try:
            makefile_text = makefile_path.read_text()
            target_re = re.compile(r"^(test|lint|check|e2e|typecheck)\s*:", re.MULTILINE)
            for match in target_re.finditer(makefile_text):
                name = match.group(1)
                if name not in seen_names:
                    gates.append(
                        {
                            "name": name,
                            "command": f"make {name}",
                            "parallel": name != "e2e",
                        }
                    )
                    seen_names.add(name)
        except Exception:
            pass

    # Check package.json
    pkg_json_path = repo_root / "package.json"
    if pkg_json_path.is_file():
        try:
            pkg = json.loads(pkg_json_path.read_text())
            scripts = pkg.get("scripts", {})
            for name in ("test", "lint", "check", "e2e", "typecheck"):
                if name in scripts and name not in seen_names:
                    gates.append(
                        {
                            "name": name,
                            "command": f"npm run {name}",
                            "parallel": name != "e2e",
                        }
                    )
                    seen_names.add(name)
        except Exception:
            pass

    # Check Package.swift (Swift Package Manager).
    if (repo_root / "Package.swift").is_file():
        if "build" not in seen_names:
            gates.append({"name": "build", "command": "swift build", "parallel": True})
            seen_names.add("build")
        if "test" not in seen_names:
            gates.append({"name": "test", "command": "swift test", "parallel": True})
            seen_names.add("test")

    return gates


def _detect_implement_commands(repo_root: Path) -> tuple[str, str]:
    """Auto-detect repo-local implement setup/teardown helpers."""
    candidates = {
        "setup": (
            "scripts/implement-setup.sh",
            "scripts/implement_setup.sh",
            "scripts/implement-setup.py",
            "scripts/implement_setup.py",
        ),
        "teardown": (
            "scripts/implement-teardown.sh",
            "scripts/implement_teardown.sh",
            "scripts/implement-teardown.py",
            "scripts/implement_teardown.py",
        ),
    }

    detected: dict[str, str] = {"setup": "", "teardown": ""}
    for kind, paths in candidates.items():
        for rel_path in paths:
            if (repo_root / rel_path).is_file():
                detected[kind] = f"python {rel_path}" if rel_path.endswith(".py") else rel_path
                break
    return detected["setup"], detected["teardown"]


def _toml_escape(s: str) -> str:
    """Escape a string for use inside a TOML double-quoted value."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _generate_spec_toml(
    *,
    base_ref: str,
    default_agent: str,
    review_default: str,
    allowed_agents: list[str],
    gates: list[dict],
    install_command: str = "",
    setup_command: str = "",
    teardown_command: str = "",
) -> str:
    """Generate .spec.toml content."""
    lines = [
        f'base_ref = "{base_ref}"',
        "",
        "[paths]",
        'specs_dir = "specs"',
        'task_specs_dir = "specs/tasks"',
        'state_dir = ".spec-state"',
        'worktrees_dir = ".worktrees"',
        "",
        "[retry]",
        "cap = 20",
        "no_progress_retry_threshold = 2",
        "",
        "[agents]",
        f'default = "{default_agent}"',
        f'review_default = "{review_default}"',
        "allowed = [{}]".format(", ".join(f'"{a}"' for a in allowed_agents)),
        "",
        "[bootstrap]",
    ]
    if install_command:
        lines.append(f'install_command = "{_toml_escape(install_command)}"')
        windows_install_command = (
            install_command.replace(" && ", "; ", 1).replace(
                ".venv/bin/python",
                r".\.venv\Scripts\python.exe",
            )
            if install_command.startswith("python -m venv .venv && .venv/bin/python ")
            else r"python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ."
        )
        lines.append(
            f'install_command_windows = "{_toml_escape(windows_install_command)}"'
        )
        lines.append('install_shell_windows = "powershell"')
    else:
        lines.append(
            '# install_command = "python -m venv .venv && '
            '.venv/bin/python -m pip install -e ."'
        )
        lines.append(
            '# install_command_windows = "python -m venv .venv; '
            '.\\\\.venv\\\\Scripts\\\\python.exe -m pip install -e ."'
        )
        lines.append('# install_shell_windows = "powershell"')

    lines += [
        "",
        "[implement]",
    ]
    if setup_command:
        lines.append(f'setup_command = "{_toml_escape(setup_command)}"')
    else:
        lines.append('# setup_command = "scripts/implement-setup.sh"')
    if teardown_command:
        lines.append(f'teardown_command = "{_toml_escape(teardown_command)}"')
    else:
        lines.append('# teardown_command = "scripts/implement-teardown.sh"')

    lines += [
        "",
        "[verify]",
    ]

    if gates:
        for gate in gates:
            lines.append("")
            lines.append("[[verify.gates]]")
            lines.append(f'name = "{_toml_escape(gate["name"])}"')
            lines.append(f'command = "{_toml_escape(gate["command"])}"')
            if gate.get("argv_windows"):
                lines.append(
                    "argv_windows = "
                    + json.dumps(gate["argv_windows"], ensure_ascii=False)
                )
            lines.append(f"parallel = {'true' if gate.get('parallel') else 'false'}")
    else:
        lines.append("")
        lines.append("# No verify gates were auto-detected.")
        lines.append("# Add your test/lint commands here:")
        lines.append("# [[verify.gates]]")
        lines.append('# name = "test"')
        lines.append('# command = "pytest"')
        lines.append("# parallel = true")

    lines.append("")
    return "\n".join(lines)


def _update_gitignore(repo_root: Path) -> bool:
    """Append spec-related entries to .gitignore. Returns True if modified."""
    gitignore_path = repo_root / ".gitignore"
    existing = ""
    if gitignore_path.is_file():
        existing = gitignore_path.read_text()

    existing_lines = set(line.strip() for line in existing.splitlines())
    entries_to_add = []
    for entry in (
        ".venv/",
        ".spec-state/",
        ".spec-workspaces/",
        ".worktrees/",
        ".spec.local.toml",
        ".claude/settings.local.json",
        ".claude/mcp-servers.json",
        ".codex/",
        ".spec-claude-home/",
        ".spec-codex-home/",
    ):
        if entry not in existing_lines:
            entries_to_add.append(entry)

    if not entries_to_add:
        return False

    separator = "" if existing.endswith("\n") or not existing else "\n"
    with open(gitignore_path, "a") as f:
        f.write(separator + "\n".join(entries_to_add) + "\n")
    return True


def _read_bundled_template(name: str) -> str:
    """Read a bundled template file from the templates directory."""
    templates = importlib.resources.files("spec_runtime") / "templates"
    return (templates / name).read_text(encoding="utf-8")


def _copy_template(repo_root: Path, template_name: str, dest_rel: str, *, force: bool) -> bool:
    """Copy a bundled template to the repo. Returns True if written."""
    dest = repo_root / dest_rel
    if dest.exists() and not force:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Repository text is part of the cross-platform contract. In particular,
    # the bundled review prompt contains Unicode punctuation; relying on the
    # Windows ANSI code page writes that punctuation as cp1252 and makes a
    # later UTF-8 review launch fail before the reviewer can start.
    dest.write_text(_read_bundled_template(template_name), encoding="utf-8")
    return True


def _build_agent_merge_command(agent: str) -> list[str]:
    """Build command to invoke agent in pipe mode for merging."""
    if agent == "claude":
        return ["claude", "-p"]
    if agent == "codex":
        return ["codex", "exec"]
    return []


def _gather_repo_context(repo_root: Path) -> str:
    """Build a text snapshot of the repo for the agent prompt."""
    sections: list[str] = []

    # Top-level directory listing (non-hidden, depth 1)
    try:
        entries = sorted(p.name for p in repo_root.iterdir() if not p.name.startswith("."))
        sections.append("--- directory listing ---\n" + "\n".join(entries))
    except OSError:
        pass

    # README (first 100 lines)
    for readme_name in ("README.md", "README.rst"):
        readme_path = repo_root / readme_name
        if readme_path.is_file():
            try:
                lines = readme_path.read_text().splitlines()[:100]
                sections.append(f"--- {readme_name} ---\n" + "\n".join(lines))
            except OSError:
                pass
            break

    # CI configs (first 50 lines each)
    ci_configs: list[Path] = []
    gh_workflows = repo_root / ".github" / "workflows"
    if gh_workflows.is_dir():
        ci_configs.extend(sorted(gh_workflows.glob("*.yml")))
        ci_configs.extend(sorted(gh_workflows.glob("*.yaml")))
    for ci_name in (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml"):
        ci_path = repo_root / ci_name
        if ci_path.is_file():
            ci_configs.append(ci_path)
    for ci_path in ci_configs:
        try:
            lines = ci_path.read_text().splitlines()[:50]
            rel = ci_path.relative_to(repo_root).as_posix()
            sections.append(f"--- {rel} ---\n" + "\n".join(lines))
        except OSError:
            pass

    # Build manifests (first 50 lines each)
    for manifest_name in (
        "Makefile",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "build.gradle",
        "CMakeLists.txt",
        "Package.swift",
    ):
        manifest_path = repo_root / manifest_name
        if manifest_path.is_file():
            try:
                lines = manifest_path.read_text().splitlines()[:50]
                sections.append(f"--- {manifest_name} ---\n" + "\n".join(lines))
            except OSError:
                pass

    # CONTRIBUTING.md (first 50 lines)
    contributing = repo_root / "CONTRIBUTING.md"
    if contributing.is_file():
        try:
            lines = contributing.read_text().splitlines()[:50]
            sections.append("--- CONTRIBUTING.md ---\n" + "\n".join(lines))
        except OSError:
            pass

    return "\n\n".join(sections)


def _build_yolo_prompt(repo_context: str) -> str:
    """Build the prompt sent to the agent for --yolo config detection."""
    example_spec = (importlib.resources.files("spec_runtime") / "examples" / "spec.toml").read_text(encoding="utf-8")

    return (
        "You are analyzing a code repository to determine its build, test, and "
        "verify configuration.\n\n"
        "Here is the reference .spec.toml schema with comments explaining every field:\n\n"
        "```toml\n"
        f"{example_spec}"
        "```\n\n"
        "Here is a snapshot of the repository:\n\n"
        f"{repo_context}\n\n"
        "I've already determined `base_ref`, `[paths]`, `[retry]`, and `[agents]` "
        "from the repo. You need to determine:\n\n"
        "1. `[bootstrap].install_command` — the command to install dependencies in a "
        "fresh worktree clone. Empty string if none needed.\n"
        "2. `[[verify.gates]]` — each gate has `name`, `command`, and `parallel` "
        "(bool). Include all test/lint/check/typecheck commands you can identify. "
        "Gates that are safe to run concurrently should have `parallel = true`. "
        "End-to-end or integration tests that need exclusive resources should have "
        "`parallel = false`.\n"
        "3. `[implement].setup_command` — a command to run before the agent starts "
        "implementing (e.g., starting services). Empty string if none needed.\n"
        "4. `[implement].teardown_command` — a command to run after implementation "
        "(e.g., stopping services). Empty string if none needed.\n\n"
        "Output **only** a JSON object (no markdown, no explanation) with this exact schema:\n\n"
        '{"install_command": "<string>", "gates": [{"name": "<string>", '
        '"command": "<string>", "parallel": <bool>}], "setup_command": "<string>", '
        '"teardown_command": "<string>"}\n\n'
        "If you cannot determine a value, use an empty string (for commands) or an "
        "empty array (for gates). Do not guess or hallucinate commands that aren't "
        "evidenced in the repo files."
    )


def _ask_agent_for_config(agent: str, prompt: str) -> dict | None:
    """Invoke the agent and parse the response. Returns config dict or None."""
    cmd = _build_agent_merge_command(agent)
    if not cmd:
        return None
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = result.stdout.strip()

    # Strip markdown fences if present
    if output.startswith("```"):
        lines = output.splitlines()
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        output = "\n".join(lines).strip()

    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return None

    # Validate expected keys
    required_keys = {"install_command", "gates", "setup_command", "teardown_command"}
    if not isinstance(data, dict) or not required_keys.issubset(data.keys()):
        return None

    if not isinstance(data["gates"], list):
        return None

    for gate in data["gates"]:
        if not isinstance(gate, dict):
            return None
        if not {"name", "command", "parallel"}.issubset(gate.keys()):
            return None
        if not isinstance(gate["name"], str) or not isinstance(gate["command"], str):
            return None
        if not isinstance(gate["parallel"], bool):
            return None

    # Validate top-level command fields are strings
    for key in ("install_command", "setup_command", "teardown_command"):
        if not isinstance(data[key], str):
            return None

    return data


def _merge_file_with_agent(
    agent: str,
    existing_content: str,
    template_content: str,
    filename: str,
) -> str | None:
    """Invoke agent to merge template into existing file.

    Returns merged content or None on failure.
    """
    prompt = (
        "Merge the following template into the existing file. "
        "Keep all existing content. "
        "Add sections from the template that are not already present. "
        "Do not remove or rewrite existing sections. "
        "Output only the merged file content, nothing else.\n\n"
        f"--- EXISTING {filename} ---\n{existing_content}\n"
        f"--- TEMPLATE {filename} ---\n{template_content}\n"
    )
    cmd = _build_agent_merge_command(agent)
    if not cmd:
        return None
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return None
        output = result.stdout
        if not output or not output.strip():
            return None
        return output
    except (OSError, subprocess.TimeoutExpired):
        return None


def cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap a repository for spec-driven development."""
    force = bool(vars(args).get("force", False))

    repo_root = _git_repo_root()
    if repo_root is None:
        print("Error: Not inside a git repository.", flush=True)
        return 1

    spec_toml = repo_root / ".spec.toml"
    config_exists = spec_toml.exists()

    if config_exists and not force:
        print(
            f"Error: {spec_toml} already exists. Use --force to refresh templates (config is preserved).",
            flush=True,
        )
        return 1

    # Detect settings
    base_ref = _detect_base_branch(repo_root)
    default_agent, allowed_agents = _detect_agents()
    review_default = "codex" if "codex" in allowed_agents else default_agent

    if not default_agent:
        print(
            "Error: No coding agents found on PATH (looked for: claude, codex). "
            "Install at least one before running spec init.",
            flush=True,
        )
        return 1

    yolo = bool(vars(args).get("yolo", False)) and not config_exists

    if yolo:
        print("  Running agent-assisted detection...", flush=True)
        repo_context = _gather_repo_context(repo_root)
        prompt = _build_yolo_prompt(repo_context)
        agent_config = _ask_agent_for_config(default_agent, prompt)
        if agent_config is None:
            print(
                "Error: Agent-assisted detection failed. Check agent availability and try again.",
                flush=True,
            )
            return 1
        install_command = agent_config["install_command"]
        gates = agent_config["gates"]
        setup_command = agent_config["setup_command"]
        teardown_command = agent_config["teardown_command"]
    else:
        gates = _detect_verify_gates(repo_root)
        install_command = _detect_install_command(repo_root)
        setup_command, teardown_command = _detect_implement_commands(repo_root)

    # Write .spec.toml — never overwrite existing config (even with --force)
    if config_exists:
        print(f"  Preserved {spec_toml.relative_to(repo_root)} (not overwritten)")
    else:
        toml_content = _generate_spec_toml(
            base_ref=base_ref,
            default_agent=default_agent,
            review_default=review_default,
            allowed_agents=allowed_agents,
            gates=gates,
            install_command=install_command,
            setup_command=setup_command,
            teardown_command=teardown_command,
        )
        spec_toml.write_text(toml_content, encoding="utf-8")
        print(f"  Created {spec_toml.relative_to(repo_root)}")

    # Create directories and spec template
    for d in ("specs", "specs/tasks"):
        (repo_root / d).mkdir(parents=True, exist_ok=True)
    # Git does not track empty directories, so without a marker file the
    # suggested `git add specs/` silently drops specs/tasks/ and a fresh clone
    # lacks the directory required by the task flow.
    gitkeep = repo_root / "specs" / "tasks" / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")
    if _copy_template(repo_root, "TEMPLATE.md", "specs/TEMPLATE.md", force=force):
        print("  Created specs/ and specs/tasks/ with TEMPLATE.md")
    else:
        print("  Created specs/ and specs/tasks/")

    # Update .gitignore
    if _update_gitignore(repo_root):
        print("  Updated .gitignore")

    # Copy templates — track skipped files so we can warn at the end
    skipped: list[str] = []

    if _copy_template(repo_root, "review.md", ".github/prompts/review.md", force=force):
        print("  Created .github/prompts/review.md")
    elif (repo_root / ".github" / "prompts" / "review.md").exists():
        skipped.append(".github/prompts/review.md")

    for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
        # Only copy agent-specific files when the agent is detected
        if name == "CLAUDE.md" and "claude" not in allowed_agents:
            continue
        if name == "CODEX.md" and "codex" not in allowed_agents:
            continue
        if _copy_template(repo_root, name, name, force=force):
            print(f"  Created {name}")
        elif (repo_root / name).exists():
            skipped.append(name)

    # Summary
    print()
    print(f"  Base branch:  {base_ref}")
    print(f"  Agents:       {', '.join(allowed_agents)} (default: {default_agent})")
    print(f"  Reviews:      {review_default}")
    if install_command:
        print(f"  Install:      {install_command}")
    else:
        print("  Install:      (none detected — edit .spec.toml [bootstrap] if needed)")
    if gates:
        print(f"  Verify gates: {', '.join(g['name'] for g in gates)}")
    else:
        print("  Verify gates: (none detected — edit .spec.toml to add)")

    # Auto-merge skipped files via detected agent
    merged: list[str] = []
    declined: list[str] = []

    if skipped:
        print()
        for path in skipped:
            # Determine template name from dest path
            template_name = path.split("/")[-1]
            prompt_msg = f"  {path} already exists. Use {default_agent} to merge spec content into it? [Y/n] "
            try:
                answer = input(prompt_msg)
            except EOFError:
                answer = "n"
            view_cmd = (
                'python -c "from spec_runtime.init import'
                " _read_bundled_template; print("
                f"_read_bundled_template('{template_name}'))\""
            )
            if answer.strip().lower() in ("", "y"):
                existing_content = (repo_root / path).read_text()
                template_content = _read_bundled_template(
                    template_name,
                )
                result = _merge_file_with_agent(
                    default_agent,
                    existing_content,
                    template_content,
                    template_name,
                )
                if result is not None:
                    (repo_root / path).write_text(result, encoding="utf-8")
                    merged.append(path)
                    print(f"    Merged {path}")
                else:
                    declined.append(path)
                    print(f"    Warning: Agent failed to merge {path}. Merge manually:")
                    print(f"      {view_cmd}")
            else:
                declined.append(path)
                print(f"    Skipped {path}. Merge manually:")
                print(f"      {view_cmd}")

    # Build list of files to include in git add
    all_changed: list[str] = [".spec.toml", ".gitignore", "specs/"]
    if ".github/prompts/review.md" not in declined:
        all_changed.append(".github/prompts/")
    for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
        if name == "CLAUDE.md" and "claude" not in allowed_agents:
            continue
        if name == "CODEX.md" and "codex" not in allowed_agents:
            continue
        if name not in declined:
            all_changed.append(name)
    # Add merged paths that aren't already covered (e.g. .github/prompts/review.md)
    for path in merged:
        if path not in all_changed:
            all_changed.append(path)

    print()
    print("Next steps:")
    if merged:
        print(f"  Agent merged: {', '.join(merged)}")
    print("  1. Commit the generated files:")
    print(f"     git add {' '.join(all_changed)}")
    print('     git commit -m "Initialize spec-driven development"')
    print("  2. Validate local readiness:")
    print("     spec doctor")
    print("  3. Create your first spec:")
    print("     spec create --spec my-first-feature")
    print("  4. Implement it:")
    print("     spec implement --spec my-first-feature")

    return 0
