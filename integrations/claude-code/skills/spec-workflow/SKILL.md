---
name: spec-workflow
description: Author, lint, dispatch, watch and take over Spec Butler workflows.
---

Work from the repository root with Spec Butler installed. Read AGENTS.md.
Author a dispatchable spec with spec create, or edit specs/<id>.md in an isolated
workspace. Include acceptance criteria and dependencies. Use lint_spec to validate.
Commit and open the spec PR targeting main, then wait for it to merge before dispatch.
Use list_specs with ready_only=true to select work whose DAG dependencies are merged.
Use implement_spec to dispatch; retain its run_id. It changes state and runs detached.
Poll spec_status to watch progress; there are no push notifications.
Use steer_run for guidance on the next attempt. When stuck, inspect status and findings.
For takeover, stop_run first, then request attach_command and run the returned command
in an interactive terminal. attach_command can restore archived sessions locally.
After operator work, resume using the repository workflow. Never dispatch another run
from inside an implement session; publishing remains owned by the orchestrator.
