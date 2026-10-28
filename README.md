# Homebrew Tap

```sh
brew tap joeblau/hb
```

| Formula | What it does | Docs |
| --- | --- | --- |
| `sr` | Local AI image super-resolution with Real-ESRGAN | [super-resolution](docs/super-resolution.md) |
| `runner-setup` | Provision and operate GitHub Actions self-hosted runners on macOS | [boot-startup](docs/boot-startup.md) |
| `agent-mcp` | MCP server that wraps any agent CLI (codex, claude, gemini, grok, kimi) | [agent-mcp](agent-mcp/README.md) |
| `appicon` | App icon generator | [joeblau/appicon](https://github.com/joeblau/appicon) |
| `simrecord` | iPhone Simulator recorder | [joeblau/simrecord](https://github.com/joeblau/simrecord) |

## Runner tooling

`runner-setup` installs these commands; each has a guide in [`docs/`](docs).
Prepare a new Mac, then add its runners:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/joeblau/homebrew-hb/main/bin/runner-bootstrap)"
runner-setup --org ORG --token TOKEN --runners 2 --colima
```

- `runner-bootstrap` — host setup: Homebrew, Brewfile, power, FileVault, SSH, Tailscale, auto-login, GUI readiness, keyboard navigation, Dock ([docs](docs/boot-startup.md))
- `runner-setup` / `runner-cleanup` — provision and tear down the runners themselves ([docs](docs/boot-startup.md))
- `runner-ephemeral` — one-job runners ([docs](docs/ephemeral-runners.md))
- `runner-prune` — checkout cleanup that keeps warm caches ([docs](docs/disk-cleanup.md))
- `runner-health` — liveness and console-session watchdog with alerts, installed by `runner-setup` ([docs](docs/health-checks.md))
- `runner-tailscale-guard` — reboots before the tailscaled kernel panic ([docs](docs/boot-startup.md))
- `runner-cache` — local S3 cache and Actions cache gateway ([docs](docs/cache-backend.md))
- `runner-bazel-cache` — Bazel remote cache ([docs](docs/bazel-cache.md))
- `runner-sccache` — compiler caches ([docs](docs/compiler-cache.md))
- `runner-ramscratch` — RAM-backed build scratch ([docs](docs/ram-scratch.md))
- `runner-bench` — CI benchmark harness ([docs](docs/runner-performance.md))
- `runner-git-mirror` — local Git mirrors ([docs](docs/git-mirror.md))
- `runner-logs` — logs, metrics and dashboard ([docs](docs/log-aggregation.md))
- `runner-upgrade` — canary upgrades and rollback ([docs](docs/auto-upgrades.md))
- `runner-autoscale` — queue-aware scaling ([docs](docs/autoscaling.md))
- `runner-token` — short-lived registration tokens ([docs](docs/token-management.md))
- `runner-netisolate` — egress policy and VPN kill-switch ([docs](docs/network-isolation.md))
- `runner-docker-builder` — fast Docker builds ([docs](docs/docker-builds.md))
- `runner-mcp` — read-only MCP server for runner diagnostics ([docs](docs/agent-tools.md))
- `runner-agent` — Claude Code as a CI-fixing agent ([docs](docs/runner-agent.md))

## Development

Scripts live in `bin/`. Run the tests with:

```sh
python3 -m unittest discover -s tests
```
