# CI-fixing agent on your own runners (`runner-agent`)

`runner-agent` runs [Claude Code](https://claude.com/claude-code) as a
background engineer on the self-hosted macOS runners this tap provisions.
GitHub Actions is the event source, the runner Mac is the sandbox, and
[`runner-mcp`](agent-tools.md) gives the agent host-level context a hosted
service cannot see. No new service, no GitHub App, no inbound ports.

| Trigger | What happens |
| --- | --- |
| A watched workflow fails on a same-repo branch | The agent reads the failed-step log, checks the runner (disk, cache, builder, health), fixes the code minimally, runs the relevant tests, and opens a PR on `agent/run-<id>`. Infrastructure problems get a diagnosis comment naming the `runner-*` command to run instead of a code change. |
| `@runner-agent <request>` on a PR or a review comment | The agent does the request on the PR branch and pushes a commit. |
| `@runner-agent <request>` on an issue | The agent does the request on `agent/issue-<n>-<comment>` and opens a PR. |

Every run ends with a comment on the originating PR, issue, or commit
containing the agent's root cause, change, verification, and runner notes,
plus cost and turn count — and, when a Slack webhook is configured, a compact
outcome notification (see [Slack notifications](#slack-notifications)).

## Setup

1. On each runner Mac, as the runner service user:

   ```sh
   brew install joeblau/hb/runner-setup
   runner-agent install        # brew install --cask claude-code + gh, then verifies
   ```

   `runner-agent check` re-verifies `claude`, `runner-mcp`, `gh`, `jq`, and `git`;
   the reusable workflow runs it as its first step.

2. In each repository, add a Claude credential as an Actions secret:

   ```sh
   gh secret set ANTHROPIC_API_KEY            # API key, or
   gh secret set CLAUDE_CODE_OAUTH_TOKEN      # from `claude setup-token`
   ```

3. Add the caller workflow and commit it:

   ```sh
   runner-agent snippet > .github/workflows/runner-agent.yml
   ```

   Edit `workflows: [ci]` to the workflow names whose failures the agent
   should handle.

4. Protect the default branch so agent PRs require a human review.

## Reusable workflow inputs

The caller passes these under `with:` to
`joeblau/homebrew-hb/.github/workflows/runner-agent.yml`:

| Input | Default | Purpose |
| --- | --- | --- |
| `runs-on` | `["self-hosted", "macOS"]` | Runner labels (JSON array). |
| `mention` | `@runner-agent` | Trigger phrase in comments. |
| `branch-prefix` | `agent/` | Prefix for branches the agent creates; runs on these branches are never re-fixed. |
| `model` | Claude Code default | Model override. |
| `max-turns` | `60` | Turn cap per invocation. |
| `max-budget-usd` | `5` | Spend cap per invocation. |
| `allowed-tools` | `Read,Edit,Write,Glob,Grep,Bash,mcp__runner__*` | Claude Code `--allowedTools`. |
| `instructions` | empty | Repository-specific instructions appended to the prompt, e.g. `Run make test before finishing.` |
| `log-lines` | `300` | Failed-step log lines fetched for a CI failure. |
| `allowed-associations` | `OWNER MEMBER COLLABORATOR` | Comment author associations that may trigger the agent. |
| `timeout-minutes` | `45` | Job timeout. |

Secrets: `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (one required), and
optionally `AGENT_GITHUB_TOKEN` (see below). Outputs: `outcome`
(`skipped`, `comment`, `pushed`, `pr`) and `pr-url`.

## Slack notifications

The agent posts best-effort, compact outcome messages to a Slack incoming
webhook: a `@runner-agent` mention is acknowledged at `gate`, and `publish`
reports the outcome — fix PR opened, fix pushed to the PR branch, or the run
diagnosed with no code change. Messages carry repo/run/PR identifiers and one
link only; no secrets, no log contents, no agent summary. A missing or
unreachable webhook is logged and never fails the job.

Setup:

1. In Slack, create an incoming webhook (api.slack.com/apps → your app →
   Incoming Webhooks → Add New Webhook to Workspace) and copy the URL.
2. On each runner Mac, as the runner service user, store it in a 0600 file:

   ```sh
   install -m 600 /dev/null ~/.runner-agent/slack-webhook
   printf '%s\n' 'https://hooks.slack.com/services/T…/B…/…' > ~/.runner-agent/slack-webhook
   ```

   `SLACK_WEBHOOK_URL` in the environment takes precedence over the file, and
   `RUNNER_AGENT_SLACK_WEBHOOK_FILE` overrides the file path. The reusable
   workflow does not forward a `SLACK_WEBHOOK_URL` secret today, so the
   per-host file is the way to wire it up; if you call the workflow from your
   own fork that sets the env var on the job, that works too.
3. The webhook URL is handed to curl through a temporary 0600 `-K` config
   file (`url = "..."`), so it never appears on a command line where `ps`
   could expose it — the same idiom `runner-logs` uses for its bearer token.

Operators and other workflows can emit their own events with
`runner-agent notify <text> [url]` (exit 2 without a message).

Out of scope, unchanged: cross-repository changes and a hosted queue (see
[runner-performance.md](runner-performance.md)). The Slack surface is
notification-only — nothing is triggered from Slack.

## How a job runs

```
runner-agent check     preflight: toolchain and credentials
runner-agent gate      read the event, decide mode/ref/branch, or skip
actions/checkout       the ref gate chose
runner-agent run       build the prompt, mount runner-mcp, `claude -p`
runner-agent publish   commit, push, PR or comment — the only write step
```

The agent is told not to commit or push and its allowed tools do not need
to include `gh`; `publish` owns every git and GitHub write, discards any edit
under `.github/workflows/`, and excludes `__pycache__`, `.DS_Store`, and
`*.pyc` from the commit. The prompt, the MCP config, Claude's JSON result,
and the summary are uploaded as the `runner-agent-<run id>` artifact.

Guard rails are enforced by `gate` inside the job, so the caller's `if:` is
only a cost-saving pre-filter:

- runs and PRs from forks are skipped;
- runs on `branch-prefix` branches, runs of the agent workflow itself, and
  runs triggered by `*[bot]` accounts are skipped;
- a failed run is attempted once (`agent/run-<id>` already existing means skip);
- comments must contain the mention, come from a non-bot user, and have an
  allowed author association.

## Trust model

- The job runs with `contents: write` and `pull-requests: write` on a machine
  that also runs your CI. Keep the trigger surface to same-repo events and
  trusted commenters, and require review before merging agent PRs.
- Log output, PR comments, and issue text are untrusted input to the agent.
  The prompt says so, `runner-mcp` is read-only, and the agent's changes only
  land through `publish`, but a determined injection could still make the
  agent write bad code into a PR. Review agent PRs like any other.
- `Bash` is in the default allowed tools so the agent can run tests. Narrow
  `allowed-tools` (for example `Bash(make:*),Bash(python3:*)`) if the
  repository's tests are always invoked the same way.
- Pushes made with the job's `GITHUB_TOKEN` never trigger other workflows, so
  CI does not run on agent PRs by default. Provide `AGENT_GITHUB_TOKEN` (a
  fine-grained PAT or GitHub App installation token with `contents` and
  `pull-requests` write) to get CI on agent PRs. The agent workflow itself
  still skips its own branches.
- Secrets reach the job only as environment variables; `runner-agent` never
  writes them to disk or prints them. Claude Code's transcript lives in the
  runner user's `~/.claude`; treat that directory as sensitive.

## Cost and tuning

Each invocation is capped by `max-turns` and `max-budget-usd`; a typical
single-file fix with a test run is a few turns and well under a dollar. The
final comment reports turns and cost. Use `instructions` to name the test
command and any repository conventions, which shortens runs more than
raising limits does.

To disable the agent for a repository, remove the caller workflow or set its
job `if:` to `false`. To pause it fleet-wide without touching every
repository, uninstall `claude` on the runner hosts; the preflight step fails
fast and nothing else runs.
