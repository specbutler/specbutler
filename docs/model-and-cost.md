# Models, usage and cost budgets

Choose models independently for each provider and role in `.spec.toml`:

```toml
[agents.models.implement]
claude = "sonnet"
codex = "gpt-5-codex"
escalate_after_attempts = 2
escalate_model = { claude = "opus", codex = "gpt-5" }

[agents.models.review]
claude = "opus"

[agents.models.debugger]
codex = "gpt-5"

[agents.models.authoring]
claude = "sonnet"

[budget]
max_cost_usd_per_run = 20.0
```

Model strings are passed unchanged to the installed CLI. Use model identifiers
supported by your provider account. Unconfigured roles pass no model flag and
retain the CLI default. Escalation applies to implementation after the configured
number of failed workflow attempts; it also applies to recovery launches. A
string `escalate_model` applies to either provider, or use a provider table as above.

The run state's `usage` array contains a receipt for each agent launch: `role`,
`provider`, `model`, `phase`, `recorded_at`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_creation_tokens`, `estimated_cost_usd`, and `usage_available`. An unknown
model or cost is null. Claude receipts retain `provider_usage`, `model_usage` and
`session_id`. Model usage includes subagents. Resuming a Claude session replaces
its effective whole-session totals with the latest result; historical receipts
remain available. Codex completed-turn usage is additive and cost is currently
null. Cached Codex input is a subset of input tokens, so do not add cache tokens
to input tokens to calculate total input.

`spec status --spec ID` shows totals for the run and each phase. `spec analytics`
includes `usage.total`, `usage.months`, `usage.roles`, and `usage.providers`, with
its existing spec, run and date filters. `unknown_cost_launches` makes missing
cost information visible rather than treating unknown costs as free.

Interactive authoring launches read usage from provider session transcripts after
exit. Claude message usage includes subagent transcripts and uses standard API
list prices for recognized models; unknown models retain null cost. Codex rollout
counters contribute only usage appended during the launch. Missing or unreadable
transcripts retain `usage_available = false` and null cost. Standalone authoring
receipts are included in analytics but excluded from workflow status, dispatch,
and garbage collection. Task scoping usage belongs to the task run and reduces
its remaining budget. Monthly analytics attribute increases in whole-session
Claude counters to the month of each launch, preserving earlier months.

Budgets are disabled by default. A configured budget checks observed estimated
cost before launches and between phases. Claude receives `--max-budget-usd` with
the remaining estimate. Reaching the limit produces a nonretryable
`budget_exceeded` failure and blocks the run. Raise or remove the budget before
resuming. Unknown costs, including Codex, cannot be enforced; this is an estimate,
not an authoritative billing cap. Under a subscription, cost is an estimate at
API list prices, not a charge against the subscription. Claude's reported cost
is used directly; no provider billing API is queried.

`spec doctor` warns when `ANTHROPIC_API_KEY` is passed to Claude: in print mode it
takes priority over subscription login. It also warns when OpenAI/Codex API keys
are present alongside a ChatGPT login. The warnings name variables without
printing secret values. Remove unintended API keys from the launch environment
to avoid silent API billing.
