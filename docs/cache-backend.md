# Local cache backend for `actions/cache` (`runner-cache`)

`runner-cache` gives GitHub Actions self-hosted runners on this Mac a fast,
local S3-compatible backend for `actions/cache`, so cache reads/writes never
leave the machine. It installs [MinIO](https://min.io) via Homebrew and runs it
as a system LaunchDaemon — the same service model
[`runner-setup`](../runner-setup) uses for the runners themselves
(`com.github.runner-N` LaunchDaemons running `bin/runsvc.sh` as the invoking
non-root user, with `sudo` only for `/opt` and `/Library/LaunchDaemons`).

## Architecture

```
workflow job (actions/cache)
        │  ACTIONS_CACHE_URL / ACTIONS_RESULTS_URL (set on the runner SERVICE)
        ▼
runner process (com.github.runner-N LaunchDaemon, user)
        │  S3 API, http://127.0.0.1:9000
        ▼
MinIO (com.github.runner-cache LaunchDaemon, same user)
        │  per-scope bucket + per-scope access keypair
        ▼
/opt/github-runner-cache/data   (cached blobs)
```

- MinIO server + client come from `brew install minio/stable/minio minio/stable/mc`.
  If Homebrew or MinIO is missing, `runner-cache install` says exactly what to
  install and stops.
- The LaunchDaemon `com.github.runner-cache` runs a tiny generated wrapper
  (`/opt/github-runner-cache/bin/run-minio.sh`) that sources a `0600` env file
  and `exec`s `minio server`. Credentials therefore never appear in the
  world-readable (`root:wheel 644`) plist and never on the `minio` command
  line (`ps`).

## Quick start

```sh
brew install joeblau/hb/runner-cache        # once the tap formula is published

# One-command deploy (installs MinIO if needed, generates credentials,
# authors + bootstraps the LaunchDaemon, creates the bucket + keypair):
runner-cache install --repo acme/monorepo
# or org-wide:
runner-cache install --org acme

# Print the runner-service configuration + wiring instructions:
runner-cache env --repo acme/monorepo
```

`install` is idempotent: re-running it reuses the existing root credentials,
daemon, and per-scope keypair, and simply ensures the scope exists.

## Wiring the runners to the cache

The runner process reads `ACTIONS_CACHE_URL`, `ACTIONS_RESULTS_URL`, and
`ACTIONS_CACHE_SERVICE_V2` **at startup** to pick its cache backend. They must
therefore be set on the runner **service**, not per-job — a workflow-level
`env:` block is too late, the runner has already chosen its backend before the
job starts.

`runner-cache env` prints the exact values, e.g.:

```sh
ACTIONS_CACHE_URL=http://127.0.0.1:9000/
ACTIONS_RESULTS_URL=http://127.0.0.1:9000/
ACTIONS_CACHE_SERVICE_V2=true
```

Add them to the `EnvironmentVariables` dict of each runner LaunchDaemon that
`runner-setup` wrote — `/Library/LaunchDaemons/com.github.runner-N.plist` —
then restart that runner:

```sh
sudo launchctl kickstart -k system/com.github.runner-1
```

Two generations of the cache protocol exist; both are covered:

- **Legacy (v1):** clients use `ACTIONS_CACHE_URL` alone.
- **v2 (results service):** `ACTIONS_CACHE_SERVICE_V2=true` +
  `ACTIONS_RESULTS_URL` selects the newer cache-results path.

Here both URLs point at the same local MinIO endpoint, so old and new clients
work without further changes.

### Workflow snippet

Once the service env vars are set, `actions/cache` is transparent — no
workflow changes are needed:

```yaml
steps:
  - uses: actions/checkout@v4

  - uses: actions/cache@v4
    with:
      path: ~/Library/Caches/Homebrew
      key: brew-${{ runner.os }}-${{ hashFiles('**/Brewfile.lock.json') }}
      restore-keys: |
        brew-${{ runner.os }}-
```

## Isolation and security

- **Per-scope buckets:** `--org acme` → bucket `actions-cache-org-acme`;
  `--repo acme/monorepo` → bucket `actions-cache-repo-acme-monorepo`. Org and
  repo caches never share a bucket, so one repo cannot read or poison another
  repo's cache.
- **Distinct access keys per scope:** `install` generates a unique
  access/secret keypair per scope and attaches an S3 policy that allows
  `s3:*` on *only* that scope's bucket (created via `mc admin policy` /
  `mc admin user`).
- **Credential storage:** root and per-scope credentials live in
  `/opt/github-runner-cache/config/*.env` with mode `0600`, owned by the
  invoking user. They are never echoed to the terminal, never written into the
  LaunchDaemon plist, and never passed on a command line where avoidable
  (`mc` receives the root credentials through an `MC_HOST_*` environment
  variable, not argv). Read them with `sudo` only when configuring external
  tooling.
- **No root services:** MinIO runs as the invoking non-root user; `sudo` is
  used only for `/opt` and `/Library/LaunchDaemons`.
- **Rotation:** delete the scope file under
  `/opt/github-runner-cache/config/scopes/` and re-run `runner-cache install`.

## Metrics

MinIO exposes a Prometheus endpoint on the API port at
`/minio/v2/metrics/cluster`. The generated config sets
`MINIO_PROMETHEUS_AUTH_TYPE=public` so local scraping needs no credential.

```sh
runner-cache metrics          # parsed summary: requests, GET/PUT counts,
                              # 4xx/5xx (misses/errors), traffic, approx. hit rate
runner-cache metrics --raw    # full Prometheus exposition
```

Point Prometheus/Grafana at `http://127.0.0.1:9000/minio/v2/metrics/cluster`
for dashboards and alerting. Metric names may vary slightly across MinIO
versions; the summary degrades gracefully and `--raw` always shows the source
of truth.

## Commands

| Command | What it does |
| --- | --- |
| `install (--org ORG \| --repo OWNER/REPO) [--port N] [--console-port N]` | Install MinIO, generate credentials, bootstrap the daemon, provision the scope. Ports are fixed at first install (defaults 9000/9001). |
| `env [--org ORG \| --repo OWNER/REPO]` | Print the `ACTIONS_*` configuration and wiring instructions (scope flags optional when only one scope exists). |
| `start` | Bootstrap/kickstart `system/com.github.runner-cache` and wait for health. |
| `stop` | Boot out the daemon; the plist stays installed. |
| `status` | Daemon state, health endpoint, buckets, disk usage — no secrets. |
| `metrics [--raw]` | Scrape the Prometheus endpoint; parsed summary or raw dump. |
| `uninstall [--yes] [--keep-data]` | Remove daemon, plist, and `/opt/github-runner-cache`; `--keep-data` preserves cached blobs. |

## Optional: remote S3 fallback

If the local backend is down or a runner runs off-host, you can fall back to a
remote S3-compatible store (e.g. AWS S3) without changing workflows:

- **Remote cache service:** point the same `ACTIONS_CACHE_URL` /
  `ACTIONS_RESULTS_URL` at an externally hosted S3-backed cache endpoint in
  the runner LaunchDaemon and `kickstart -k` the runner. Workflows are
  untouched because the backend choice lives on the service.
- **Warm standby:** replicate the local buckets to remote S3 with
  `mc mirror --watch cache/actions-cache-… remote/…` so a failover endpoint
  already holds recent entries.
- **Per-workflow override:** a drop-in cache action such as `runs-on/cache`
  can target S3 directly via `AWS_*` env vars for jobs that must bypass the
  local backend — but prefer the service-level env vars so the standard
  `actions/cache` stays transparent.

## Troubleshooting

- `runner-cache status` — one-shot health/launchd/bucket/disk overview.
- `sudo launchctl print system/com.github.runner-cache` — launchd's view.
- Logs: `/opt/github-runner-cache/logs/minio-stdout.log` and
  `minio-stderr.log`.
- Console UI: `http://127.0.0.1:9001` (root credentials in
  `/opt/github-runner-cache/config/minio.env`, mode `0600`).
- If the runner does not pick up the backend: confirm the `ACTIONS_*` keys are
  in the runner plist's `EnvironmentVariables` and that you ran
  `sudo launchctl kickstart -k system/com.github.runner-N` afterwards.
