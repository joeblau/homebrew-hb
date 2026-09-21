# Persistent compiler cache (`runner-sccache`)

`runner-sccache` wires [sccache](https://github.com/mozilla/sccache) into
workflows on trusted GitHub Actions jobs on one Mac. Preserved package
*downloads* (the S3 dependency cache in [cache-backend.md](cache-backend.md))
do not prevent *recompilation*: `runner-prune` removes checkout-local build
output under `_work` between jobs, and nothing else in this tap configures a
compiler cache. `runner-sccache` closes that gap with an **opt-in** cache on
persistent SSD storage **outside `_work`**, so compiled objects survive
ephemeral workspace resets and warm rebuilds hit the cache instead of
recompiling.

Each scope (`--org` or `--repo`) gets its **own** cache directory under
`/opt/github-runner-sccache/data/<slug>`, its **own** loopback-only server
port, and its **own** size cap, recorded in a 0600 scope config under
`/opt/github-runner-sccache/config/scopes`. Scope slugs carry a digest of the
canonical org/repository identity, so punctuation, owner/repository
separators, and long names never collapse into one cache directory. This
mirrors `runner-bazel-cache`'s per-scope isolation; see
[bazel-cache.md](bazel-cache.md) for the Bazel remote cache instead.

Unlike `runner-cache` and `runner-bazel-cache`, there is **no LaunchDaemon**.
sccache's client auto-starts a per-user server with the environment of the
build that invokes it, so the per-scope `SCCACHE_*` settings emitted by
`runner-sccache env` *are* the service definition. sudo is used only to
create `/opt/github-runner-sccache`, which is then owned by your normal user.

## Install and configure

`runner-sccache` is included in the `runner-setup` formula:

```sh
brew install joeblau/hb/runner-setup
runner-sccache install --repo acme/monorepo
runner-sccache env --repo acme/monorepo
```

Use `--org acme` when every repository sharing the cache belongs to the same
trust boundary. Installation runs `brew install sccache` as your normal user
when the command is absent.

Each new scope is allocated the first free loopback port starting at 4226;
pin one with `--port N` (1024–65535, not used by another installed scope).
The per-scope size cap defaults to 20 GiB; set it with `--max-size GIB`
(1–4096). Ports and caps are fixed at first install; reinstalling a scope
reuses them — uninstall the scope first to change either.

`env` emits only these non-secret settings on stdout:

```sh
SCCACHE_BIN=/opt/homebrew/bin/sccache
SCCACHE_DIR=/opt/github-runner-sccache/data/repo-acme-monorepo-<digest>
SCCACHE_CACHE_SIZE=20G
SCCACHE_SERVER_PORT=4226
SCCACHE_IDLE_TIMEOUT=0
RUSTC_WRAPPER=/opt/homebrew/bin/sccache
```

Local mode has **no credentials** — the cache is reachable only from this
Mac's loopback and only by jobs running as the runner user. `env` also prints
a workflow snippet on stderr. GitHub does not consume these values
automatically; the workflow step below activates them.

## Workflow integration

Cache configuration stays **workflow-owned**: `runner-sccache` never rewrites
workflow files. Append the scope settings to `$GITHUB_ENV` in one early step,
then build as usual.

Rust / Cargo:

```yaml
- name: Configure sccache
  run: runner-sccache env --repo acme/monorepo >> "$GITHUB_ENV"
- name: Build
  run: cargo build --locked --release
```

Cargo picks up `RUSTC_WRAPPER` automatically; every `rustc` invocation routes
through sccache.

C / C++ with CMake:

```yaml
- name: Configure sccache
  run: runner-sccache env --repo acme/monorepo >> "$GITHUB_ENV"
- name: Build
  run: |
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_C_COMPILER_LAUNCHER=sccache \
      -DCMAKE_CXX_COMPILER_LAUNCHER=sccache
    cmake --build build
```

Other C/C++ build systems can wrap the compiler explicitly after the env
step: `export CC="sccache cc" CXX="sccache c++"`.

**Always export `SCCACHE_DIR`, `SCCACHE_CACHE_SIZE`, and
`SCCACHE_SERVER_PORT` together** (the `$GITHUB_ENV` step does). The per-scope
port is what keeps two repository scopes from silently sharing one sccache
server — and therefore one cache. Two scopes on the same Mac run two servers
on two ports against two cache directories; neither can read the other's
results through its own endpoint. Parallel jobs within one scope share the
scope's server safely — sccache serializes cache access, and identical
compilations produce identical hits.

### What is cached — and what is not

sccache hashes the compiler binary, the (preprocessed) source, all
compilation flags, and the target into every cache key, so **changing the
source, compiler, flags, or target produces a correct cache miss**, never a
stale hit. Only compilations are cached; **linking is never cached**.

Unsupported by sccache, therefore **not accelerated**:

- **Xcode / Swift builds.** `swiftc` is not a supported compiler.
  Language-specific caches such as Xcode's derived data and the Swift module
  cache require separate integrations and are *not* accelerated by sccache.
- Compiler invocations sccache cannot parse (unusual flag combinations,
  `-arch` multi-arch outputs, some `-Xclang` uses). sccache detects these and
  runs the real compiler directly; they show up as non-cacheable calls, not
  errors.
- Anything outside C/C++/Rust (and CUDA via nvcc) — sccache's supported set.

For Objective-C/C++ specifically, ccache (configured entirely in the
workflow) may cover more clang invocations; see the ccache/sccache notes in
[bazel-cache.md](bazel-cache.md#ccache-and-sccache).

## Limits, eviction, lifecycle, and precedence

- **Size limits and eviction.** Each scope's cache is bounded by its
  `--max-size` cap; sccache evicts least-recently-used entries past the cap,
  so storage growth is bounded per scope without any external cleanup.
- **Permissions.** Cache directories are 0700 and scope configs 0600, all
  owned by the invoking non-root user. These permissions do **not** isolate
  jobs running as the same macOS user — use this for trusted workloads on a
  dedicated Mac, and do not point untrusted fork jobs at a scope's cache.
- **Lifecycle.** `install` provisions; `env` activates per workflow;
  `stop` stops the scope's server without touching its cache (the next build
  restarts a server); `uninstall` stops the server and deletes the cache and
  config (`--keep-data` preserves both for reinstall; removing the last scope
  deletes `/opt/github-runner-sccache`).
- **Configuration precedence.** Settings resolve in this order: (1) the
  `SCCACHE_*` environment exported by your workflow's `env` step — highest;
  (2) a user-level sccache config file (`~/.config/sccache/config`), if you
  create one; (3) sccache's built-in defaults. The tool never writes a global
  sccache config and never changes unrelated workloads: nothing happens to a
  build whose workflow does not opt in.

## Failure fallback

A cache failure never breaks a build. If the sccache server is unreachable,
the disk cache is corrupt, or a cache read/write fails, sccache runs the real
compiler directly and counts the failure under cache errors — the build stays
correct, just uncached. Watch the error counters with `runner-sccache stats`;
a rising error count means you are paying wrapper overhead without caching.
To bypass the cache entirely, drop the `env` step from the workflow or unset
`RUSTC_WRAPPER` — both restore a plain uncached build with no other change.

## Statistics and benchmarks

`runner-sccache stats [--org ORG | --repo OWNER/REPO]` surfaces
`sccache --show-stats` for the scope — compile requests, cache hits, cache
misses, cache errors (and write errors) — plus disk usage against the cap.
`--json` emits one object for scripts and benchmark output:

```json
{"tool":"runner-sccache","slug":"repo-acme-monorepo-<digest>","kind":"repo","scope":"acme/monorepo","port":4226,"server_reachable":true,"compile_requests":100,"cache_hits":60,"cache_misses":30,"cache_errors":1,"cache_write_errors":0,"disk_usage_kb":1234567,"max_size_gib":20}
```

If no build has run yet (or the server was stopped), counters report as 0
with `server_reachable:false`; disk usage remains accurate.

To demonstrate the acceptance criterion — **compiler cache hits after an
ephemeral workspace reset** — run a cold build, wipe the checkout the way an
ephemeral runner would, rebuild warm, and record both wall times plus stats:

```yaml
- name: Configure sccache
  run: runner-sccache env --repo acme/monorepo >> "$GITHUB_ENV"
- name: Cold build
  run: cargo build --locked --release
- name: Simulate ephemeral workspace reset
  run: rm -rf target
- name: Stats before warm rebuild
  run: runner-sccache stats --repo acme/monorepo --json
- name: Warm rebuild (expect cache hits)
  run: time cargo build --locked --release
- name: Stats after warm rebuild
  run: runner-sccache stats --repo acme/monorepo --json
```

The cache lives outside `_work`, so deleting `target` (or the whole checkout)
does not touch it; the warm rebuild should show rising hits and a much
shorter wall time. Changing a source file, the toolchain, flags, or target
should show rising misses — that is sccache validating its inputs correctly.

## Cleanup coordination

`sccache`'s internal LRU eviction runs continuously and is **safe while jobs
are active** — it never removes entries a running compile still needs beyond
forcing a recompile on the next miss. Manual deletion of a scope's data
directory is *not* safe mid-build: stop the scope's server first
(`runner-sccache stop`), and perform bulk eviction only inside the explicit
offline maintenance window `runner-prune --all --offline` already requires.
`runner-sccache` deliberately does not hook into `runner-prune`, and
`runner-prune` does not know about `/opt/github-runner-sccache`; the size
cap, not the pruner, bounds growth during normal operation.

## Shared backend (documented configuration path)

Local caching on this Mac is the supported first step. A **shared** backend
across multiple Macs is intentionally *not wired* by this tool, but sccache
supports S3 and the MinIO store installed by `runner-cache` is a suitable
target. To adopt it, configure the workflow environment yourself (precedence
rule 1 above), reusing the scope credentials from
[cache-backend.md](cache-backend.md):

```yaml
env:
  SCCACHE_BUCKET: actions-cache-repo-acme-monorepo-<digest>   # or a dedicated bucket
  SCCACHE_ENDPOINT: 127.0.0.1:9000
  SCCACHE_S3_USE_SSL: "off"
  AWS_ACCESS_KEY_ID: ${{ secrets.RUNNER_CACHE_ACCESS_KEY }}
  AWS_SECRET_ACCESS_KEY: ${{ secrets.RUNNER_CACHE_SECRET_KEY }}
```

With an S3 backend, `SCCACHE_DIR` (and this tool's per-scope directories and
size caps) no longer apply — the bucket is the cache, and the S3 key
namespace replaces per-scope directory isolation. Keep separate buckets (or
key prefixes) per trust boundary, exactly as you would keep separate local
scopes. This is a documented path only: `runner-sccache` installs and
operates the local-disk cache, and its `stats` counters remain valid with
either backend.

## Command reference

| Command | Behavior |
| --- | --- |
| `install (--org ORG \| --repo OWNER/REPO) [--port N] [--max-size GIB]` | Install sccache, allocate the scope's port, and create its bounded cache directory and 0600 scope config. |
| `env [--org ORG \| --repo OWNER/REPO]` | Print the per-scope `SCCACHE_*`/`RUSTC_WRAPPER` settings and a workflow snippet. Scope can be omitted if exactly one is installed. |
| `stats [--org ORG \| --repo OWNER/REPO] [--json]` | Report compile requests, hits, misses, errors, and disk usage; `--json` for benchmark output. |
| `stop [--org ORG \| --repo OWNER/REPO]` | Stop the scope's sccache server; cache data is untouched. Best-effort. |
| `uninstall [--org ORG \| --repo OWNER/REPO] [--yes] [--keep-data]` | Stop the server and delete the scope's cache and config. `--keep-data` preserves both for reinstall. Removing the last scope deletes `/opt/github-runner-sccache`. |
