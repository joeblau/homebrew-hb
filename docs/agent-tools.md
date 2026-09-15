# Agent-readable diagnostics (`--json` and `runner-mcp`)

Two layers let a coding agent answer "why is runner-2 slow", "what failed in
the last job", or "is the builder cache full" without parsing human output:

1. **`--json` flags** on the read-only commands, usable from any script.
2. **`runner-mcp`**, a stdio [MCP](https://modelcontextprotocol.io) server that
   wraps those commands as tools for Claude Code, Codex, Cursor, and similar
   clients.

Everything on this page is read-only. Nothing restarts a runner, prunes a
cache, or changes Docker state; an agent that finds a problem reports which
`runner-*` command fixes it and the operator runs it.

## Machine-readable commands

| Command | Output |
| --- | --- |
| `runner-health --once --json [--dry-run]` | One document per pass: `{"ts","host","prog","ok","runners":[...]}`. Each runner object is the same record the plain log line carries: `runner`, `event` (`check` when healthy, else the issue such as `daemon_not_loaded`, `diag_stale`, `not_listening`, `offline`, `stuck_busy`, `unregistered`, `maintenance`), `svc_state`, `diag_age_min`, `api_status`, `busy`, `action`, `result` (`ok`, `remediated`, `failed`, `detected`, `skipped`). |
| `runner-logs metrics --once` | Already JSON lines: `runner`, `runner_name`, `daemon_loaded`, `status` (`idle`/`busy`/`offline`/`unknown`), `last_job_name`, `last_job_result`, `disk_*_kb`, `disk_used_pct`, `queue_queued`, `queue_in_progress`. |
| `runner-logs jobs [--runner N] [--limit N] --json` | `{"host","jobs":[{"runner","job","result","started","duration_s","file"}]}`, newest first, parsed from `Worker_*.log` completion lines. |
| `runner-logs tail [--runner N] [--kind runner\|worker\|stdout\|stderr] [--lines N] --json` | `{"runner","kind","file","mtime","lines":[...]}`. `file` is `null` and `lines` empty when that log does not exist yet. |
| `runner-cache status --json` | `{"daemon","installed","launchd_loaded","healthy","endpoint","console","data_dir","disk_usage_kb","buckets"}`. `buckets` is `null` when MinIO's admin client is unavailable (unknown) and `[]` when there are none. Never prompts for sudo; never prints credentials. |
| `runner-docker-builder status --json` | `{"context","selected_builder","mode","builders":[{"name","driver","status","selected"}],"cache_total","colima":{"installed","running"}}`. `mode` is `remote`, `colima`, `desktop`, `other`, or `unknown`; `cache_total` is BuildKit's own total for a provisioned `runner-*` builder, else `null`. |

`runner-health --dry-run` reports every issue with `result="detected"` and the
action the watchdog would take, without restarting, alerting, or clearing
alert markers. Its exit status is 1 when anything was detected. Progress and
warnings still go to stderr in every mode, so pipe stdout alone into `jq`.

Log lines are escaped for JSON: backslashes, quotes, and tabs are escaped and
other control characters (ANSI colour codes in Worker output, carriage
returns) are dropped.

## `runner-mcp`

`runner-mcp` speaks newline-delimited JSON-RPC 2.0 over stdin/stdout and
advertises these tools:

| Tool | Runs | Notes |
| --- | --- | --- |
| `runner_health` | `runner-health --once --dry-run --json` | Optional `org`/`repo` and `stale_minutes`; API checks need `GITHUB_PAT` in the server's environment. Uses sudo. |
| `runner_metrics` | `runner-logs metrics --once` | Uses sudo. |
| `runner_jobs` | `runner-logs jobs --json` | Optional `runner`, `limit` (1–500). |
| `runner_logs_tail` | `runner-logs tail --json` | Optional `runner`, `kind`, `lines` (1–5000). `kind: worker` holds the failed step's output. |
| `cache_status` | `runner-cache status --json` | |
| `docker_builder_status` | `runner-docker-builder status --json` | |
| `gh_failed_run_log` | `gh run list` + `gh run view --log-failed` | Optional `repo` (inferred from a repo-scoped runner's `.runner` file), `run_id`, `lines`. Needs an authenticated `gh`. |

Every argument is validated against a strict pattern before it reaches a
command line; invalid input returns a tool error, never a shell. Sibling
commands are located in the directory that holds `runner-mcp` (the Homebrew
`bin`), then on `PATH`; `RUNNER_MCP_BIN_DIR` overrides both.

### Register with a client

```sh
brew install joeblau/hb/runner-setup        # installs runner-mcp with the rest
claude mcp add --scope user runner -- runner-mcp
claude mcp list                             # shows "runner"
runner-mcp --list                           # print the tool catalogue
```

Any MCP client that launches stdio servers works the same way; point it at the
`runner-mcp` executable with no arguments. Run it as the runner service user on
the runner host — the tools read `/opt/github-runners`, the runner user's
Docker config, and the local MinIO service.

Then, in a session on that Mac:

- "Use runner_health, then runner_logs_tail with kind worker for any runner
  that is not idle, and tell me what the last job was doing."
- "Compare runner_jobs durations for the `build` job over the last 20 runs."
- "Is the Docker builder cache close to the Colima disk size?"
- "Show me the failed step of the latest CI run and the runner it ran on."

### sudo without a terminal

`runner_health` and `runner_metrics` read the launchd system domain through
`sudo launchctl print`. Without a terminal there is no way to type a password,
so the tool returns sudo's error instead of hanging. To let them run
unattended, allow only that read-only subcommand for the runner service user
(replace `ci` with that account) via `sudo visudo -f /etc/sudoers.d/runner-readonly`:

```
ci ALL=(root) NOPASSWD: /bin/launchctl print system, /bin/launchctl print system/com.github.runner-*
```

Both `runner-health` and `runner-logs metrics` probe `sudo -n launchctl print
system` first and stay non-interactive when it succeeds; otherwise they fall
back to a normal `sudo -v` prompt for interactive use. The rule does not grant
`bootstrap`, `bootout`, or `kickstart`, so remediation still needs an operator.

### Security notes

- All tools are read-only and never pass agent-supplied strings to a shell.
- `gh_failed_run_log` uses whatever `gh auth` identity the server process has;
  an agent can read any run log that identity can read.
- Worker logs can contain anything a job printed. Treat tool output as data:
  an agent should not follow instructions found inside log lines.
- The server exposes host state to whichever client launches it. Register it
  in a user or project scope you control; do not expose it over a network.
