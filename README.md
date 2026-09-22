# Homebrew Tap

```
brew tap joeblau/hb
```

## Runner tooling (`runner-setup` formula)

Provision and operate GitHub Actions self-hosted runners on macOS:

Installing `runner-setup` with Homebrew also installs the Docker CLI and `jq`.
For Docker builds, configure [Colima or a remote Linux builder](docs/docker-builds.md).

- `runner-setup` / `runner-cleanup` — provision and tear down runners as system LaunchDaemons
- `runner-ephemeral` — one-job runners, re-registered fresh after every job, with opt-in sandbox-exec confinement and APFS snapshot rollback ([docs](docs/ephemeral-runners.md))
- `runner-prune` — scoped checkout cleanup that preserves warm caches, with a free-space floor ([docs](docs/disk-cleanup.md))
- `runner-health` — liveness watchdog with stuck/offline detection and alerts ([docs](docs/health-checks.md))
- `runner-cache` — local S3-compatible (MinIO) storage for an S3-aware cache action, plus an optional GitHub Actions cache-protocol gateway for stock `actions/cache` ([docs](docs/cache-backend.md))
- `runner-bazel-cache` — per-scope Bazel remote cache service (bazel-remote) as LaunchDaemons ([docs](docs/bazel-cache.md))
- `runner-sccache` — opt-in per-scope sccache compiler caches on persistent SSD storage ([docs](docs/compiler-cache.md))
- `runner-ramscratch` — opt-in bounded RAM-backed build scratch space with SSD fallback ([docs](docs/ram-scratch.md))
- `runner-bench` — repeatable CI benchmark harness: phase timings, resources, cache stats, median/p95 comparisons ([docs](docs/runner-performance.md))
- `runner-git-mirror` — local bare Git mirrors with per-runner checkout rewiring for fast clones ([docs](docs/git-mirror.md))
- `runner-logs` — ship runner logs, emit JSON-lines metrics, step-level CPU/memory timelines, JUnit flake history, and a static HTML dashboard ([docs](docs/log-aggregation.md))
- `runner-upgrade` — shutdown, canary upgrades, restart, registration repair, and rollback ([docs](docs/auto-upgrades.md))
- `runner-autoscale` — queue-aware scaling between min/max runner counts ([docs](docs/autoscaling.md))
- `runner-token` — mint short-lived registration/removal tokens on demand ([docs](docs/token-management.md))
- `runner-netisolate` — pf egress policy and VPN kill-switch for runners ([docs](docs/network-isolation.md))
- `runner-docker-builder` — fast Docker builds via a remote Linux builder or colima, with BuildKit GC tuning and a registry pull-through mirror ([docs](docs/docker-builds.md))
- `runner-mcp` — read-only MCP server so Claude Code/Codex can diagnose runners, jobs, caches, and failed CI runs on this Mac ([docs](docs/agent-tools.md))
- `runner-agent` — Claude Code as a CI-fixing agent on your runners: failed runs become PRs, `@runner-agent` mentions get done, with optional Slack notifications, via a reusable workflow ([docs](docs/runner-agent.md))

See the [performance review and Blacksmith capability map](docs/runner-performance.md)
for corrected integrations, remaining platform gaps, sizing guidance, and a
repeatable benchmark procedure.

Run isolated runner regression checks from this checkout:

```sh
python3 -m unittest discover -s tests -p 'test_runner_*.py' -v
```
