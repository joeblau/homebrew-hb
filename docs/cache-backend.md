# Local S3 dependency cache (`runner-cache`)

`runner-cache` installs MinIO for dependency caches shared by trusted GitHub
Actions jobs on one Mac. It runs as the invoking non-root user through the
`com.github.runner-cache` LaunchDaemon and stores objects under
`/opt/github-runner-cache/data`. The S3 API and console listen on loopback only.

**MinIO is not a replacement endpoint for `actions/cache`.** GitHub's current
cache client uses authenticated cache service RPCs, which differ from S3.
Changing `ACTIONS_CACHE_URL` or `ACTIONS_RESULTS_URL` to a MinIO address cannot
make that protocol work. Use an action that explicitly supports S3, or keep
GitHub's built-in caching. See the [GitHub cache client](https://github.com/actions/toolkit/blob/main/packages/cache/src/internal/shared/cacheTwirpClient.ts).

## Install and configure

`runner-cache` is included in the `runner-setup` formula:

```sh
brew install joeblau/hb/runner-setup
runner-cache install --repo acme/monorepo
runner-cache env --repo acme/monorepo
```

Use `--org acme` instead when every repository using the bucket belongs to the
same trust boundary. Installation uses the official
[MinIO Homebrew tap](https://github.com/minio/homebrew-stable) for
`minio/stable/minio` and `minio/stable/mc` when those commands are absent.
Homebrew runs as your normal user. An unrelated Midnight Commander `mc` must
be unlinked or removed from the command search path first.

The API and console default to ports 9000 and 9001. Choose different, unused
ports with `--port` and `--console-port` on the first install; both must be
between 1024 and 65535. Reinstalling reuses configured ports and credentials,
rewrites the wrapper, and restarts MinIO, so perform it while cache clients
are idle. This is not a rolling or interruption-free upgrade.

`env` emits only these non-secret settings on stdout, with the actual bucket
name substituted:

```sh
RUNNER_CACHE_ENDPOINT=127.0.0.1
RUNNER_CACHE_PORT=9000
RUNNER_CACHE_BUCKET=actions-cache-repo-acme-monorepo-<digest>
RUNNER_CACHE_INSECURE=true
```

It also prints a workflow example and the scope credential file path on
stderr. These `RUNNER_CACHE_*` values are descriptive S3 settings; GitHub does
not consume them automatically.

## Give the workflow its scope credentials

As the service owner, read the credential file identified by `env` and copy
`ACCESS_KEY` and `SECRET_KEY` into the repository's Actions secrets named
`RUNNER_CACHE_ACCESS_KEY` and `RUNNER_CACHE_SECRET_KEY`. For example, run the
following in Bash after replacing the file path and repository:

```bash
set +x  # Do not trace credential expansion.
source "/opt/github-runner-cache/config/scopes/repo-acme-monorepo-<digest>.env"
printf '%s' "$ACCESS_KEY" | gh secret set RUNNER_CACHE_ACCESS_KEY --repo acme/monorepo
printf '%s' "$SECRET_KEY" | gh secret set RUNNER_CACHE_SECRET_KEY --repo acme/monorepo
unset ACCESS_KEY SECRET_KEY
```

Use only scope credentials, never `MINIO_ROOT_USER` or `MINIO_ROOT_PASSWORD`.
For organization secrets, restrict repository access to the intended users
of that org bucket. Setup intentionally does not upload secrets itself.

## Workflow integration

Copy the concrete snippet from `runner-cache env`. A workflow after checkout
can use the following template, replacing the bucket with its printed value:

```yaml
- uses: tespkg/actions-cache@v1
  id: dependency-cache
  with:
    endpoint: 127.0.0.1
    port: 9000
    insecure: true
    accessKey: ${{ secrets.RUNNER_CACHE_ACCESS_KEY }}
    secretKey: ${{ secrets.RUNNER_CACHE_SECRET_KEY }}
    bucket: actions-cache-repo-acme-monorepo-<digest>
    use-fallback: false
    path: ~/Library/Caches/Homebrew
    key: brew-${{ github.repository }}-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('**/Brewfile.lock.json') }}
```

The third-party [tespkg/actions-cache](https://github.com/tespkg/actions-cache)
action supports MinIO, separate restore/save actions, `restore-keys`, and
optional GitHub fallback. Its [input definition](https://github.com/tespkg/actions-cache/blob/main/action.yml)
specifies separate host/port inputs and the Node 24 runtime. Use runner
2.327.1 or newer for Node 24 actions; this requirement is also documented by
[actions/cache](https://github.com/actions/cache#whats-new). Review and pin a
full action commit SHA in production instead of a moving major tag.

`use-fallback: false` keeps this example local. Set it to `true` explicitly if
GitHub cache fallback on S3 operation failure is desired; it is not S3 bucket
replication. Plain `actions/cache`, language setup actions' built-in caches,
and Docker `type=gha` still use GitHub's service. Disable duplicate caching
in a language setup action when explicitly caching the same directory here.
Docker layers need their own [BuildKit cache configuration](docker-builds.md).

Use a dependency lockfile that actually exists: an unmatched `hashFiles`
pattern produces no useful dependency version in the key. Include repository,
OS, architecture, and relevant toolchain versions for compiled caches. Cache
package downloads selectively; archiving an entire home directory or a huge
build tree can cost more time than fetching dependencies again. Compare cold
and warm runs and record both restore/save time and total job duration.

## Correct earlier endpoint overrides

Earlier versions of this guide incorrectly directed GitHub cache endpoints
to MinIO. If you followed those instructions, drain the runner and remove the
manually added `ACTIONS_CACHE_URL`, `ACTIONS_RESULTS_URL`, and
`ACTIONS_CACHE_SERVICE_V2` entries from its plist's `EnvironmentVariables`.
Remove any manual `ACTIONS_RUNTIME_TOKEN` override as well. Preserve values
provided by GitHub inside jobs. GitHub identifies changes to these variables
as a source of [cache migration failures](https://github.blog/changelog/2025-03-20-notification-of-upcoming-breaking-changes-in-github-actions/).

Reload each edited service when it is idle so launchd reads the changed plist:

```sh
sudo launchctl bootout system/com.github.runner-1
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-1.plist
```

A `kickstart` alone restarts the loaded job definition; it does not reload an
edited plist. Add the explicit S3 action above to opt into the local cache.
Existing unambiguous legacy scope files are reused on reinstall.

## Scope boundaries and operations

New scope names contain a digest of the canonical org/repository identity,
so punctuation, owner/repository separators, and long names do not collapse
into the same bucket. Each scope gets a generated keypair with an S3 policy
limited to its bucket. An org scope deliberately shares its bucket across the
repositories granted its credentials.

The wrapper exports MinIO's root credentials and metrics configuration before
starting the process. Config directories are mode 0700 and credential files
are 0600; no credential values are written into the world-readable daemon
plist or printed by `env`. Root credentials are loaded inside the `mc` child
process; new user credentials go through stdin using the supported
[MinIO client interface](https://github.com/minio/mc/blob/master/cmd/admin-user-add.go).

These filesystem permissions and S3 policies **do not isolate jobs running as
the same macOS user**: those jobs can read that user's credential files and
local caches. Use this service for trusted workloads on a dedicated Mac.
S3 keys also do not reproduce GitHub's branch/fork authorization rules; keys
and bucket credentials determine who can read and write. Do not give
untrusted fork jobs these credentials. A Docker VM cannot reach this host
service through its own `127.0.0.1`.

To rotate credentials, revoke the old scope user in MinIO first, remove its
scope credential file, reinstall that scope, and update its GitHub secrets.
Deleting the file alone does not revoke the existing user. Cached objects
persist across rotation.

| Command | Behavior |
| --- | --- |
| `install (--org ORG \| --repo OWNER/REPO)` | Install dependencies, configure the daemon, and provision a scope. |
| `env [--org ORG \| --repo OWNER/REPO]` | Print S3 settings and workflow instructions without secrets. Scope can be omitted if exactly one is installed. |
| `start` / `stop` | Start or stop MinIO; `start` fails if health does not recover. |
| `status` | Report daemon, health, buckets, and disk usage. |
| `metrics [--raw]` | Report S3 request/error/traffic counters, or raw Prometheus output. |
| `uninstall [--yes] [--keep-data]` | Remove the daemon. `--keep-data` preserves **both data and credentials/config** for reinstall; otherwise all cache files are removed. |

Local metrics are at `http://127.0.0.1:9000/minio/v2/metrics/cluster`; public
Prometheus authentication is limited by the loopback binding. S3 GET counts
and HTTP errors cannot establish workflow hit rates. Record the cache action's
`cache-hit` output instead. Inspect `metrics --raw` when a MinIO release changes
its metric names.

Use `runner-cache status`, `sudo launchctl print system/com.github.runner-cache`,
and `/opt/github-runner-cache/logs/minio-stderr.log` for troubleshooting. Monitor
disk usage and configure bucket lifecycle retention for your workload; this
helper does not currently impose a cache size cap or automatically expire
objects. For remote S3, configure a reachable TLS endpoint and its own scope
credentials in the S3 action; the locally installed server intentionally
listens only on this Mac.
