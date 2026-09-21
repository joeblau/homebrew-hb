# Bazel remote cache (`runner-bazel-cache`)

`runner-bazel-cache` installs [bazel-remote](https://github.com/buchgr/bazel-remote)
for Bazel builds running on trusted GitHub Actions jobs on one Mac. Each scope
(`--org` or `--repo`) gets its **own** system LaunchDaemon
(`com.github.runner-bazel-cache-<slug>`) running as the invoking non-root user,
its own cache directory under `/opt/github-runner-bazel-cache/data/<slug>`, its
own loopback-only HTTP port, its own size cap, and its own generated basic-auth
credential. This mirrors `runner-cache`'s shape; see
[cache-backend.md](cache-backend.md) for the S3 dependency cache instead.

bazel-remote speaks Bazel's remote cache protocol (action cache + CAS). It is
not related to `actions/cache` and does not change any `ACTIONS_*` runner
configuration.

## Install and configure

`runner-bazel-cache` is included in the `runner-setup` formula:

```sh
brew install joeblau/hb/runner-setup
runner-bazel-cache install --repo acme/monorepo
runner-bazel-cache env --repo acme/monorepo
```

Use `--org acme` when every repository sharing the cache belongs to the same
trust boundary. Installation runs `brew install bazel-remote` as your normal
user when the command is absent; sudo is used only for `/opt` and
`/Library/LaunchDaemons`.

Each new scope is allocated the first free port starting at 9092; pin one with
`--port N` (1024–65535, not used by another installed scope). The per-scope
size cap defaults to 20 GiB; set it with `--max-size GIB` (1–4096).
bazel-remote evicts least-recently-used blobs once a scope's directory exceeds
its cap, so disk usage is bounded per scope. Ports and caps are fixed at first
install; reinstalling a scope reuses its port, cap, and credentials, rewrites
the wrapper, and restarts the daemon — do it while Bazel clients are idle.

`env` emits only these non-secret settings on stdout:

```sh
RUNNER_BAZEL_CACHE_ENDPOINT=127.0.0.1
RUNNER_BAZEL_CACHE_PORT=9092
RUNNER_BAZEL_CACHE_MAX_SIZE_GIB=20
RUNNER_BAZEL_CACHE_REMOTE_CACHE_FLAG=--remote_cache=http://127.0.0.1:9092
```

It also prints a workflow snippet and the scope credential file path on stderr.
GitHub does not consume these `RUNNER_BAZEL_CACHE_*` values automatically.

## Give the workflow its scope credentials

As the service owner, read the credential file identified by `env` and copy
`REMOTE_CACHE_URL` — which embeds the scope's username and password — into the
repository's Actions secret named `BAZEL_REMOTE_CACHE_URL`. For example, run
the following in Bash after replacing the file path and repository:

```bash
set +x  # Do not trace credential expansion.
source "/opt/github-runner-bazel-cache/config/scopes/repo-acme-monorepo-<digest>.env"
printf '%s' "$REMOTE_CACHE_URL" | gh secret set BAZEL_REMOTE_CACHE_URL --repo acme/monorepo
unset REMOTE_CACHE_URL
```

For organization secrets, restrict repository access to the intended users of
that org cache. Setup intentionally does not upload secrets itself.

## Workflow integration

Cache configuration stays **workflow-owned**: `runner-bazel-cache` never
rewrites workflow files or `.bazelrc`. The recommended pattern commits no
credentials — a step expands the secret into a local bazelrc at runtime:

```yaml
- run: printf 'build --remote_cache=%s\n' "$BAZEL_REMOTE_CACHE_URL" >> .bazelrc
  env:
    BAZEL_REMOTE_CACHE_URL: ${{ secrets.BAZEL_REMOTE_CACHE_URL }}
```

Alternatively, commit the loopback endpoint (`build
--remote_cache=http://127.0.0.1:9092`) and supply credentials at runtime with
`--remote_header=Authorization=Basic ...` or a CI-only bazelrc — but never
commit the authenticated URL.

Useful additions for CI builds: `--remote_timeout=10` so a stalled cache never
blocks a build, and `--remote_download_toplevel` for jobs that only need final
artifacts. `--remote_upload_local_results` is on by default; leave it enabled
so jobs populate the cache. Remote caching deduplicates work across jobs on
this Mac; it does not replace Bazel's on-disk repository cache, which already
persists between jobs.

Compare cold and warm `bazel build` wall times and record both. If hit rates
disappoint, the cap may be too small for the working set (reinstall the scope
with a larger `--max-size`), or builds may not be reproducible enough for
action-cache hits — check for timestamps or absolute paths leaking into action
inputs.

## ccache and sccache

These compiler caches are configured entirely in the workflow; this tap does
not install or manage them. Guidance for workflows on persistent self-hosted
Macs:

- **ccache** (C/C++/Objective-C): `brew install ccache` in a setup step, point
  `CCACHE_DIR` at a persistent path outside the workspace (for example
  `$RUNNER_WORKSPACE/../_ccache` or `~/Library/Caches/ccache` on a dedicated
  trusted runner), cap it with `ccache --max-size=10G`, and prepend Homebrew's
  compiler-shim directory (`export PATH="$(brew --prefix)/opt/ccache/libexec:$PATH"`)
  so compilations route through it. Persist it across
  jobs either by keeping it off the cleaned workspace or by caching it with
  the S3 action from [cache-backend.md](cache-backend.md). Inspect
  effectiveness with `ccache --show-stats`.
- **sccache** (Rust, plus C/C++): `brew install sccache`, set
  `RUSTC_WRAPPER=sccache` for Cargo builds, point `SCCACHE_DIR` at a persistent
  location, and cap with `SCCACHE_CACHE_SIZE=10G`. sccache can also use S3 or
  Redis backends; for a single Mac the local disk backend is simplest.
  `sccache --show-stats` reports hit rates.
- Disable duplicate caching (for example `cache: false` on setup actions) when
  you cache the same artifacts explicitly, and scope keys by toolchain version
  so compiler upgrades do not poison the cache.

Unlike the Bazel cache above, these tools have no per-scope isolation on the
host: any job running as the runner user can read and write the shared
`CCACHE_DIR`/`SCCACHE_DIR`. Only use them for trusted workloads.

## Scope boundaries and operations

Scope slugs contain a digest of the canonical org/repository identity, so
punctuation, owner/repository separators, and long names do not collapse into
the same cache directory. Each scope's daemon binds its own 127.0.0.1 port and
requires its own generated username/password via an htpasswd file (0600);
config directories are mode 0700 and credential files 0600. No credential
values are written into the world-readable daemon plist, printed by `env`, or
placed on the bazel-remote command line (the daemon's wrapper sources the 0600
scope file; health checks pass credentials to curl via stdin config, not argv).

These permissions, ports, and credentials **do not isolate jobs running as the
same macOS user**: those jobs can read that user's credential files and local
cache directories. Use this service for trusted workloads on a dedicated Mac.
Basic-auth credentials do not reproduce GitHub's branch/fork authorization
rules; do not give untrusted fork jobs these credentials. A Docker VM cannot
reach this host service through its own `127.0.0.1`.

To rotate a scope's credentials, uninstall the scope (without `--keep-data`),
reinstall it, and update the `BAZEL_REMOTE_CACHE_URL` GitHub secret. Cached
blobs are deleted with the scope; rotation starts the cache cold.

| Command | Behavior |
| --- | --- |
| `install (--org ORG \| --repo OWNER/REPO) [--port N] [--max-size GIB]` | Install bazel-remote, generate credentials, and bootstrap the scope's daemon. |
| `env [--org ORG \| --repo OWNER/REPO]` | Print endpoint settings and workflow instructions without secrets. Scope can be omitted if exactly one is installed. |
| `start` / `stop` [--org ORG \| --repo OWNER/REPO] | Start or stop scope daemons; without a scope flag, all installed scopes. `start` fails if health does not recover. |
| `status [--json]` | Report per-scope daemon, health, size cap, and disk usage; `--json` emits `{"tool","installed","data_root","scopes":[{"slug","kind","scope","endpoint","launchd_loaded","healthy","disk_usage_kb","max_size_gib"}]}` for scripts and agents. |
| `uninstall [--org ORG \| --repo OWNER/REPO] [--yes] [--keep-data]` | Remove one scope's daemon. `--keep-data` preserves its data and credentials for reinstall; otherwise they are removed. Removing the last scope deletes `/opt/github-runner-bazel-cache`. |

For troubleshooting use `runner-bazel-cache status`,
`sudo launchctl print system/com.github.runner-bazel-cache-<slug>`, and
`/opt/github-runner-bazel-cache/logs/<slug>-stderr.log`. bazel-remote serves a
JSON status page at `http://127.0.0.1:<port>/status` (with the scope's
credentials) and, when started with metrics flags, Prometheus counters; this
helper keeps the default configuration minimal. For a shared remote cache
across multiple Macs, run bazel-remote on a reachable TLS host and store its
authenticated URL in the same `BAZEL_REMOTE_CACHE_URL` secret; the locally
installed daemons intentionally listen only on this Mac.
