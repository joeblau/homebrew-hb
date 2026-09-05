# Homebrew Tap

```
brew tap joeblau/hb
```

## Runner tooling (`runner-setup` formula)

Provision and operate GitHub Actions self-hosted runners on macOS:

- `runner-setup` / `runner-cleanup` — provision and tear down runners as system LaunchDaemons
- `runner-ephemeral` — one-job runners, re-registered fresh after every job ([docs](docs/ephemeral-runners.md))
- `runner-prune` — scoped checkout cleanup that preserves warm caches, with a free-space floor ([docs](docs/disk-cleanup.md))
- `runner-health` — liveness watchdog with stuck/offline detection and alerts ([docs](docs/health-checks.md))
- `runner-cache` — local S3-compatible (MinIO) storage for an S3-aware cache action ([docs](docs/cache-backend.md))
- `runner-logs` — ship runner logs and emit JSON-lines metrics ([docs](docs/log-aggregation.md))
- `runner-upgrade` — automated runner version upgrades with canary + rollback ([docs](docs/auto-upgrades.md))
- `runner-autoscale` — queue-aware scaling between min/max runner counts ([docs](docs/autoscaling.md))
- `runner-token` — mint short-lived registration/removal tokens on demand ([docs](docs/token-management.md))
- `runner-netisolate` — pf egress policy and VPN kill-switch for runners ([docs](docs/network-isolation.md))
- `runner-docker-builder` — fast Docker builds via a remote Linux builder or colima ([docs](docs/docker-builds.md))

See the [performance review and Blacksmith capability map](docs/runner-performance.md)
for corrected integrations, remaining platform gaps, sizing guidance, and a
repeatable benchmark procedure.

Run isolated runner regression checks from this checkout:

```sh
python3 -m unittest discover -s tests -p 'test_runner_*.py' -v
```
