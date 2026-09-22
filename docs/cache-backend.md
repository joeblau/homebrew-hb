# Local and shared S3 dependency cache (`runner-cache`)

`runner-cache` installs MinIO for dependency caches shared by trusted GitHub
Actions jobs on one Mac. It runs as the invoking non-root user through the
`com.github.runner-cache` LaunchDaemon and stores objects under
`/opt/github-runner-cache/data`. The S3 API and console listen on loopback only.

To share one cache across several Macs, `runner-cache client` configures a
**configuration-only client** for an existing authenticated S3-compatible
endpoint — no local MinIO installation or daemon on the client Mac. See
[Shared backend across Macs (client mode)](#shared-backend-across-macs-client-mode).
Local mode and its loopback defaults are unchanged either way.

**MinIO is not a replacement endpoint for `actions/cache`.** GitHub's current
cache client uses authenticated cache service RPCs, which differ from S3.
Changing `ACTIONS_CACHE_URL` or `ACTIONS_RESULTS_URL` to a MinIO address cannot
make that protocol work. Use an action that explicitly supports S3, keep
GitHub's built-in caching, or install the optional
[GitHub-protocol gateway](#github-actions-cache-protocol-gateway-optional),
which implements those RPCs in front of MinIO. See the
[GitHub cache client](https://github.com/actions/toolkit/blob/main/packages/cache/src/internal/shared/cacheTwirpClient.ts).

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
Existing unambiguous legacy scope files are reused on reinstall. The supported
way to redirect `ACTIONS_CACHE_URL` / `ACTIONS_RESULTS_URL` is the managed
[gateway](#github-actions-cache-protocol-gateway-optional) below, not manual
overrides pointed at MinIO.

## GitHub Actions cache protocol gateway (optional)

`runner-cache gateway install --repo acme/monorepo` runs a second LaunchDaemon,
`com.github.runner-cache-gateway`, alongside MinIO. It is a Python-stdlib HTTP
server (emitted to `/opt/github-runner-cache/bin/cache-gateway.py` at install
time) that implements the GitHub Actions cache service Twirp v2 JSON endpoints
— `CreateCacheEntry`, `FinalizeCacheEntry`, `GetCacheEntryDownloadURL`, and
`DeleteCacheEntry` under
`/twirp/github.actions.results.api.v1.CacheService/` — backed by the same
per-scope MinIO buckets. It listens on loopback only (default port 9157,
change with `--gateway-port` on first install). Archives are stored as
`<key>/<version>` objects in the scope bucket; the gateway answers RPCs with
SigV4 presigned URLs, so upload and download bytes flow directly between the
job and MinIO and never pass through the gateway process.

`gateway install` mints a per-scope bearer token into the scope's 0600 env
file (`GATEWAY_TOKEN`), reusing it on reinstall. A token authenticates gateway
calls and maps to exactly one scope's bucket and credentials; one gateway
serves every scope that has a token. `gateway env` prints the non-secret
wiring on stdout:

```sh
ACTIONS_CACHE_URL=http://127.0.0.1:9157/
ACTIONS_RESULTS_URL=http://127.0.0.1:9157/
ACTIONS_CACHE_SERVICE_V2=true
```

and on stderr explains how to append those lines — plus
`ACTIONS_RUNTIME_TOKEN` set to the scope's `GATEWAY_TOKEN` value — to a
runner's `.env` file (mode 0600, next to `run.sh`), then restart that runner
while idle. Standard `actions/cache` (v4, service v2) then stores entries in
the local bucket. Caveats:

- `ACTIONS_RUNTIME_TOKEN` is also used by GitHub's cache, artifact, and
  results clients, so GitHub-hosted cache/artifact calls fail on a wired
  runner. Wire only runners dedicated to local caching.
- Every job on a wired runner inherits the token, including PR jobs. Only
  wire runners that run trusted workflows; keep untrusted fork jobs on
  unwired runners.
- The explicit S3 action flow (`tespkg/actions-cache`) is unaffected and both
  integrations can run side by side.

### Branch authorization and its limits

`gateway install --save-prefix PREFIX` (repeatable) writes the scope's SAVE
policy to `config/scopes/<slug>.save-policy`: one allowed cache-key prefix per
line, `*` alone allows every key (the default), and restore is never
restricted — matching `actions/cache` `restore-keys` semantics, where any ref
may read caches written by permitted keys. `CreateCacheEntry`,
`FinalizeCacheEntry`, and `DeleteCacheEntry` require a matching prefix;
`GetCacheEntryDownloadURL` does not. Convention: embed the branch in keys,
e.g. `key: brew-${{ github.ref_name }}-${{ hashFiles('**/Brewfile.lock.json') }}`,
and set `--save-prefix main-` so only keys naming the default branch can save.

Be honest about what this cannot enforce:

- The cache protocol carries no branch or ref field. The gateway sees only the
  key string, so "branch authorization" is key-prefix policy by naming
  convention. A job can claim any prefix it is allowed to use.
- The entry `version` is an opaque client-computed hash of the key, paths, and
  compression settings; the gateway cannot verify that an uploaded archive
  matches the key it claims. Any job holding the runner's token can write
  arbitrary content under an allowed prefix — cache poisoning is bounded by
  the prefix policy, not eliminated.
- Forks and pull requests on a wired runner share that runner's token. The
  policy file plus runner dedication is the whole boundary; do not treat it as
  GitHub's per-ref cache isolation.

### Gateway operations and scope

`gateway start` / `gateway stop` manage the daemon; `gateway status [--json]`
reports daemon, health (`GET /healthz`), and authorized scope count without
secrets. `runner-cache uninstall` also removes the gateway daemon and plist.

Distributed operation is intentionally **not implemented** for the gateway:
one gateway and one MinIO per Mac, loopback only, no cross-host cache sharing,
and no reachability from GitHub-hosted runners or Docker VMs. For cross-host
cache sharing, point the other Macs at a reachable S3 endpoint with
[`runner-cache client`](#shared-backend-across-macs-client-mode) and the
explicit S3 action instead.

## Shared backend across Macs (client mode)

Local mode binds MinIO to `127.0.0.1`, so jobs on another Mac cannot reuse
this host's cache. `runner-cache client` is the supported cross-host mode: it
records a **shared, already-authenticated S3-compatible endpoint** per scope
and emits the workflow integration for it. It is configuration-only — it
installs no MinIO, starts no daemon, and performs no reachability checks, so
it works on a Mac with no cache software installed at all.

### Private-network deployment

Run the S3 server (MinIO or any S3-compatible service) on one dedicated,
always-on host reachable from every runner Mac over the trusted LAN or VPN.
Terminate TLS there (a certificate the runner Macs trust) and keep the server
off the public internet with firewall rules; TLS is the default in client
mode and `--no-tls` is intended only for a trusted private network. Do **not**
turn a local-mode Mac into the shared server by changing its bind address:
local mode intentionally binds loopback, and exposing it would also expose
its console and credentials. Provision one bucket and one constrained
keypair per scope on the server, exactly as local mode does.

On each client Mac, configure the scope with the endpoint settings the
supported S3 client (the minio-js-based action) needs:

```sh
printf 'ACCESS_KEY=%s\nSECRET_KEY=%s\n' "$AK" "$SK" \
  | runner-cache client install --repo acme/monorepo \
      --endpoint cache.internal.lan --port 443 --region us-east-1
runner-cache client env --repo acme/monorepo
```

`--tls` is the default (port 443); `--no-tls` defaults the port to 9000.
`--path-style on` (the default) matches MinIO and the S3 action's addressing;
set `--path-style off` only for endpoints requiring virtual-hosted buckets.
`--bucket NAME` overrides the default `actions-cache-<scope>` name when the
server provisioned a different bucket. Credentials are read from stdin into a
managed 0600 file, or referenced in place with `--credentials-file PATH` (an
existing 0600 file with `ACCESS_KEY`/`SECRET_KEY` lines, managed by you).
Configuration and credential files live under
`/opt/github-runner-cache/config/clients/`; values are never printed, never
on argv, and never in generated workflow config. `client uninstall --repo …`
removes the configuration (and only the managed credential file); the remote
bucket's objects are untouched.

`client env` prints the same `RUNNER_CACHE_*` settings as local `env`, plus
`RUNNER_CACHE_REGION` and `RUNNER_CACHE_PATH_STYLE`, and a workflow snippet
pointing the explicit S3 action at the remote endpoint:

```yaml
- uses: tespkg/actions-cache@v1
  id: dependency-cache
  with:
    endpoint: cache.internal.lan
    port: 443
    insecure: false
    region: us-east-1
    accessKey: ${{ secrets.RUNNER_CACHE_ACCESS_KEY }}
    secretKey: ${{ secrets.RUNNER_CACHE_SECRET_KEY }}
    bucket: actions-cache-repo-acme-monorepo-<digest>
    use-fallback: false
    path: ~/Library/Caches/Homebrew
    key: brew-${{ github.repository }}-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('**/Brewfile.lock.json') }}
```

Distribute the scope's `ACCESS_KEY`/`SECRET_KEY` from the server to each
client Mac's credential file and to the same GitHub Actions secrets as in
local mode. Everything in [Workflow integration](#workflow-integration) —
pinning the action by SHA, `use-fallback`, duplicate setup-* caching — still
applies.

### Cache keys, concurrent writers, and failure behavior

- **Key inputs.** Scope keys by repository, OS, architecture, toolchain, and
  dependency lockfiles, e.g.
  `key: <kind>-${{ github.repository }}-${{ runner.os }}-${{ runner.arch }}-<toolchain>-${{ hashFiles('**/lockfile') }}`.
  The bucket already isolates the org/repository scope; the key isolates
  everything else. A job on Mac A populates an entry that a fresh workspace
  on Mac B reuses only when both compute the same key.
- **Concurrent writers.** S3 gives per-object atomicity with last-writer-wins
  replacement: readers always get a complete old or new object, never a torn
  mix. Jobs racing to save the same key are therefore safe; use per-ref or
  per-lockfile keys to keep entries useful. The action verifies the archive
  while restoring, so a failed or partial transfer fails the step instead of
  silently restoring corrupt content.
- **Backend outage.** Behavior is explicit, never silent: with
  `use-fallback: false` the cache step fails on an unreachable endpoint
  (fail the job, or add `continue-on-error` and keep a correct uncached build
  path); with `use-fallback: true` the action falls back to GitHub's cache
  service. Choose per workflow and record which you rely on.
- **Hits vs. S3 counters.** Workflow cache hits come from the action's
  `cache-hit` output and the step's restore/save durations. Raw S3 request,
  error, and byte counters on the server (its Prometheus endpoint, or
  `runner-cache metrics` on a local-mode server) are a separate lower-level
  signal and cannot establish hit rates — keep the two apart in dashboards.
- **Local/remote interaction.** Both modes can coexist on one Mac (separate
  config trees under `config/scopes/` and `config/clients/`); each workflow
  chooses its backend by the endpoint it passes to the action. A host can
  serve its own local cache and consume a remote one for different scopes.
- **Retention and size limits.** `runner-cache` imposes no size cap or object
  expiry in either mode. On the shared server, configure bucket lifecycle
  rules (for MinIO, `mc ilm rule add`) sized to the fleet's aggregate cache
  volume — several Macs write more than one — and monitor disk usage there.

### sccache integration point

The compiler-cache helper (`runner-sccache`, tracked separately) targets the
same backend through sccache's S3 storage: `SCCACHE_BUCKET=<bucket>`,
`SCCACHE_ENDPOINT=<host>:<port>` (no scheme), `SCCACHE_REGION=<region>`,
`SCCACHE_S3_USE_SSL=true|false` matching `--tls`, and
`SCCACHE_NO_PATH_STYLE=true` only when the endpoint was configured with
`--path-style off`; credentials via `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` from the scope credential file. Client mode does not
install or manage sccache; this is only the documented hand-off.

### Measuring remote cache cost

The measurement harness is tracked separately (runner performance tooling);
until it exists, measure manually. For a representative workflow, record from
the action step on both a populating Mac and a fresh-workspace Mac: the
`cache-hit` output, restore and save step durations (and, for sccache, its
statistics), plus full-job duration, across cold (empty/purged prefix) and
warm runs, and again with the endpoint unreachable to confirm the chosen
outage behavior. Compare remote-restore latency against local-mode restore
and against a fully uncached build before rolling a shared cache out.

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
| `status [--json]` | Report daemon, health, buckets, and disk usage; `--json` for scripts and agents ([schema](agent-tools.md)). |
| `metrics [--raw]` | Report S3 request/error/traffic counters, or raw Prometheus output. |
| `gateway install (--org ORG \| --repo OWNER/REPO) [--gateway-port N] [--save-prefix P]...` | Install the GitHub-protocol cache gateway daemon and mint the scope's bearer token. |
| `gateway env [--org ORG \| --repo OWNER/REPO]` | Print ACTIONS_CACHE_URL/ACTIONS_RESULTS_URL wiring for standard actions/cache; token stays in the 0600 scope file. |
| `gateway start` / `gateway stop` / `gateway status [--json]` | Manage the gateway daemon. |
| `client install (--org ORG \| --repo OWNER/REPO) --endpoint HOST [--port N] [--tls \| --no-tls] [--region R] [--path-style on\|off] [--bucket NAME] [--credentials-file PATH]` | Record a shared remote S3 endpoint and 0600 credential file for the scope. No MinIO installation or daemon. Credentials come from stdin or a referenced file, never argv. |
| `client env [--org ORG \| --repo OWNER/REPO]` | Print the remote S3 settings and the S3-action workflow integration; secrets stay in files. |
| `client uninstall (--org ORG \| --repo OWNER/REPO)` | Remove the scope's client configuration and managed credential file; remote objects are untouched. |
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
objects. For a backend shared by several Macs, keep this local server on
loopback and configure the other Macs with
[`runner-cache client`](#shared-backend-across-macs-client-mode) against a
dedicated, TLS-protected S3 endpoint.
