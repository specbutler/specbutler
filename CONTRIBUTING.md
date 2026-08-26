# Contributing to Spec Butler

This project accepts contributions as GitHub issues rather than unsolicited
code pull requests. Reports and proposals become specs, and maintainers
implement accepted work through the orchestrator.

## How to contribute

1. Search existing issues and pull requests for related work.
2. Open an issue using the bug or feature template.
3. Describe the problem, impact, and reproduction steps clearly. Proposed
   solutions and tradeoffs are welcome when they are presented as options.
4. Wait for a maintainer to triage the issue; do not open a code pull request.
5. Do not include credentials, private repository contents, `.spec-state/`,
   generated workspaces, or local agent configuration.

## What makes a good issue

The best issues explain a situation, its impact, and how to reproduce it.
Implementation suggestions are useful when they include constraints and
tradeoffs rather than assuming a particular design is mandatory.

Good:
> When I run `spec implement` on a repo with slow CI, the verify phase
> retries immediately and overwhelms the CI system. This wastes resources
> and makes runs fail with rate limit errors.

Also useful:
> Add exponential backoff to the verify retry loop. Use 1s, 2s, 4s, 8s
> intervals capped at 60s. Make it configurable in .spec.toml.

The first version is sufficient for triage. The second can help design work if
the values are a measured constraint or a starting point rather than a fixed
requirement.

## Creating issues from agents

Agents and humans can create issues with the GitHub CLI:

```bash
gh issue create --repo specbutler/specbutler \
  --title "Verify retries overwhelm CI on slow repos" \
  --label triage
```

The command opens an editor for the issue body. Use the repository's issue
templates when filing through the browser.

## What happens after you file

1. **Triage** — a maintainer reviews the issue and may ask clarifying questions
2. **Spec** — an agent reads the issue, designs a solution, and writes a spec
3. **Implementation** — `spec implement` runs the lifecycle: implement, verify,
   review, merge
4. **Closure** — the issue is closed with a link to the merged PR

## Why issues first

The repository is also the primary dogfood environment for `spec`. Routing
changes through specs exercises the workflow that the project exists to build
and keeps implementation, verification, and independent review consistent.
