---
id: model-and-cost
area: backend
priority: 30
depends_on: [agent-sessions]
description: Choose models per role, persist token and cost usage, and stop runs that exceed a budget
---

# Model and Cost Control

## Goal

No launch passes a model today, so every role uses the CLI default. Cost is printed
to stderr for Claude only and never saved; Codex usage isn't recorded at all. On a
subscription, quota is the real constraint: the owner moved accounts for credits, and
runs died on usage limits. Let operators choose models per role, record usage per run
and phase, and stop a run before it burns through a budget.

## Acceptance Criteria

- [ ] **Models per role.** A `[agents.models]` table in `.spec.toml` sets a model
  for each role: `implement`, `review`, `debugger`, `authoring`, keyed by provider.
  Optional `escalate_after_attempts` and `escalate_model` switch the implement model
  after N failed attempts. Models are passed as `--model` for Claude and `--model`/`-m`
  for Codex. When nothing is set, nothing is passed, so behaviour doesn't change.
- [ ] **Usage is saved on the run state for every agent launch:** role, provider,
  model, input/output/cache tokens and estimated cost.
  - **Claude:** `total_cost_usd`, `usage` and `modelUsage` from the result event.
    `modelUsage` includes subagents. On resumed sessions, use the latest result's
    whole-session numbers rather than summing.
  - **Codex:** token usage from `exec --json` events. Cost may be null.
- [ ] **`spec status --spec ID` shows usage** per phase and per run.
  **`spec analytics` shows totals** per month, per role and per provider.
- [ ] **Cost budget.** `[budget] max_cost_usd_per_run` (an estimate, off by default)
  stops a run with a typed `budget_exceeded` failure that blocks rather than retries.
  For Claude launches, `--max-budget-usd` is passed with the remaining budget.
- [ ] **`spec doctor` warns about silent API billing:**
  - when `ANTHROPIC_API_KEY` is present in the environment passed to Claude (in `-p`
    it takes priority over subscription login);
  - when `OPENAI_API_KEY` or `CODEX_API_KEY` is present alongside a ChatGPT login.
- [ ] **Docs** cover model selection and the usage fields, and note that cost under a
  subscription is an estimate at API list prices.
- [ ] **Hermetic tests cover:** model flags per role, escalation, usage parsing from
  fixture events for both providers, budget enforcement, and the doctor warnings.

## Out of Scope

- Authoritative billing data from provider usage APIs.
- Automatic model choice by task difficulty.

## Design Notes

- Today cost is printed but not saved: `src/spec_runtime/orchestrator.py`
  (~18764–18776).
- The provider environment allowlist includes both `ANTHROPIC_API_KEY` and
  `CLAUDE_CODE_OAUTH_TOKEN`: `src/spec_runtime/provider_env.py` (~92–112).
- Depends on `agent-sessions` because resumed sessions change how usage
  accumulates, and both touch the launch builders.
