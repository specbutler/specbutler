"""Spec-driven development workflow CLI and orchestrator runtime.

Public entry points:
- ``spec_runtime.cli:main`` — the ``spec`` CLI (installed as ``spec`` command)
- ``spec_runtime.orchestrator:main`` — the internal orchestrator (legacy compat)

Adapter boundaries:
- ``spec_runtime.forge`` — forge adapter protocol + GitHub implementation
- ``spec_runtime.agent_adapter`` — agent adapter protocol + Claude/Codex implementations
"""
