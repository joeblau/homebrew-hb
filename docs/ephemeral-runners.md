# Ephemeral self-hosted runners

Ephemeral runners are GitHub Actions self-hosted runners that pick up **exactly
one job** and are then automatically removed by GitHub. This supervisor resets the workspace and registration between jobs. The
host, user home, package caches, processes, and credentials outside `_work`
remain shared. It is intended for trusted workloads; the opt-in
[sandboxing and snapshot rollback](#opt-in-hardening) below narrow, but do not
close, that gap — a disposable VM or host is still needed to fully reset those
resources for untrusted jobs.

The GitHub Actions runner supports this natively via `config.sh --ephemeral`:
the runner unregisters itself and exits as soon as its one job completes. What
it does *not* do is come back — something has to re-register it for the next
job. That something is `runner-ephemeral`.

## How it works

`runner-setup --ephemeral` provisions runners exactly as usual, but authors the
system LaunchDaemon (`com.github.runner-N`) with `ProgramArguments` pointing at
the `runner-ephemeral` supervisor instead of `bin/runsvc.sh` directly:

```
launchd (system/com.github.runner-N, KeepAlive, ThrottleInterval=10)
  └─ runner-ephemeral --dir /opt/github-runners/runner-N --org ORG --labels ...
       loop:
         1. read name/url from .runner (previous registration's identity)
         2. POST /orgs/ORG/actions/runners/registration-token  (fresh token, GITHUB_PAT)
         3. wipe _work/ and .runner/.credentials* (keep operator .env/.path
            and the persistent _toolcache — see below)
         4. config.sh --unattended --ephemeral --replace (same name/labels/url)
         5. bin/runsvc.sh in the foreground  →  runs ONE job  →  exits
         → loop again
```

- **Identity is preserved.** The GitHub runner name and URL are read from the
  saved `.runner` JSON before wiping (same `agentName`/`gitHubUrl` sed-parsing
  as `runner-setup`), and labels are passed through from `runner-setup`, so the
  runner reappears in your org/repo with the same name and labels every cycle.
- **One job per registration.** Because every cycle registers with
  `--ephemeral`, GitHub removes the runner from the Runners list the moment its
  job finishes; the supervisor immediately mints a fresh registration.
- **Failure handling.** A failed token fetch, cleanup, `config.sh`, or runner service retries with
  exponential backoff (10s → 20s → 40s … capped at 300s). After 5 consecutive
  failures the supervisor exits non-zero, and launchd's `KeepAlive` +
  `ThrottleInterval` restarts it — the daemon self-heals without crash-looping.
  The opt-in hardening hooks below plug into the same semantics: a sandbox
  that cannot be prepared fails the cycle (backoff), and a snapshot failure
  falls back to the rm-based cleanup rather than skipping it.
- **Logging.** Every lifecycle event (cycle start, token obtained, wipe,
  registration, job exit code, backoff, shutdown) goes to stderr, which the
  LaunchDaemon redirects to `/opt/github-runners/runner-N/_diag/runner-stderr.log`.
  The runner's own logs stay in `_diag/` as usual.

## The GitHub PAT

Registration tokens expire after ~1 hour, so the supervisor mints a fresh one
per cycle using a personal access token from the `GITHUB_PAT` environment
variable. Required scopes:

| Runner scope | PAT scope (classic) | Fine-grained equivalent |
| ------------ | ------------------- | ----------------------- |
| Org runner (`--org`) | `admin:org` | Organization → Self-hosted runners: write |
| Repo runner (`--repo`) | `repo` | Repository → Administration: write |

The PAT reaches the supervisor in one of two ways (env wins):

1. `runner-setup --ephemeral` with `GITHUB_PAT` set writes it to
   `/opt/github-runners/runner-N/.github_pat`, mode `600`, owned by the runner
   user (piped via stdin — never in `ps` arguments, never in the plist, since
   plists are world-readable).
2. `GITHUB_PAT` in the supervisor's own environment (e.g. when running it
   manually for testing).

The PAT is passed through curl's standard-input configuration as an
`Authorization: Bearer` header; it is never logged or included in curl argv.
The short-lived registration token is passed to `config.sh` as an argument
(`config.sh` has no env alternative), so it is briefly visible in `ps` to other
local users — the same trade-off `runner-setup` already makes.

## Persistent tool cache

Plain `_work` wipes also deleted `_work/_tool`, so every `actions/setup-*`
step re-downloaded its toolchain after each ephemeral reset. The tool cache
now lives **outside** `_work` and survives job resets.

**Runner contract (verification basis).** The runner resolves its tool cache
directory from the first set of `RUNNER_TOOL_CACHE`, `RUNNER_TOOLSDIRECTORY`,
`AGENT_TOOLSDIRECTORY`, falling back to `_work/_tool` — see
[`HostContext.GetDirectory(WellKnownDirectory.Tools)`](https://github.com/actions/runner/blob/main/src/Runner.Common/HostContext.cs)
in actions/runner. `JobRunner` creates that directory and exposes it to every
job step as `RUNNER_TOOL_CACHE` (and as the `runner.tool_cache` context),
which is exactly where the `setup-*` actions install and look up tools (the
`AGENT_TOOLSDIRECTORY` override is also what the setup-* READMEs document for
self-hosted runners). `runsvc.sh` only loads `.path`, so the variable must
come from the service environment — that is why it is wired through the
LaunchDaemon plist and the supervisor's own export rather than `.env`. This
was verified from the upstream source above; this repo's tests mock the
runner and cannot exercise the real binary.

**Location and ownership.** The default is the per-runner
`/opt/github-runners/runner-N/_toolcache`, created and owned by the runner
user. `runner-setup` exports it via `AGENT_TOOLSDIRECTORY` in the
LaunchDaemon's `EnvironmentVariables` (override with
`runner-setup --tool-cache-dir DIR`), and `runner-ephemeral` resolves and
exports the same variable itself — so even an older plist running a newer
supervisor gets the per-runner cache. A pre-set `RUNNER_TOOL_CACHE` or
`AGENT_TOOLSDIRECTORY` in the supervisor's environment wins (the path must be
absolute and outside `RUNNER_DIR/_work`, otherwise the cycle fails and backs
off). Checkout, `_temp`, and registration residue still reset every cycle;
only the tool cache and `_diag` logs persist.

**Concurrency.** The per-runner default means two runners never write the
same cache, so concurrent jobs cannot corrupt each other's tool installs or
evict an active cache. Pointing several runners at one shared
`--tool-cache-dir` is an explicit opt-in: `setup-*` actions tolerate
concurrent reads, but concurrent first-time installs of the same tool version
can race — share only if you accept that.

**Migration (idle-only).** On the first wipe after this feature lands, a
legacy `_work/_tool` is *moved* (never copied, never merged, never deleted by
the move) to the configured cache while the runner service is stopped —
`wipe_state` runs between jobs, so the migration cannot race a running job.
If the target already exists or the legacy path is a symlink, nothing is
migrated and the legacy copy resets with `_work`. Rollback: stop the runner,
remove `AGENT_TOOLSDIRECTORY` from the plist (or the supervisor environment),
and move `_toolcache` back to `_work/_tool` — the runner then falls back to
`_work/_tool` and no cache content was deleted at any point.

**Eviction and removal.** `runner-prune` keeps the cache warm by default;
`runner-prune --all --offline --purge-caches` evicts every
`runner-N/_toolcache` during the acknowledged maintenance window (see
[disk-cleanup.md](disk-cleanup.md)). `runner-cleanup` deletes the cache with
the runner directory. `runner-upgrade` carries `_toolcache` across upgrades
and rollbacks exactly like `_work`, and `runner-upgrade repair` rewrites only
the plist's `ProgramArguments`, so the configured location survives
re-registration (see [auto-upgrades.md](auto-upgrades.md)).

**Hardening interplay.** With `--sandbox`, a tool cache outside the runner
tree is automatically added to the writable paths. With
`--snapshot-rollback`, only `_work` is restored from the snapshot — the cache
is untouched either way.

## Usage

Provision three ephemeral org runners:

```sh
GITHUB_PAT=ghp_... runner-setup --org acme --token AYZ... --runners 3 --ephemeral
```

(`--token` is still required for the initial registration; after that the
supervisor fetches its own tokens.)

Everything else works as before: `runner-cleanup` tears the runners down
(daemon, gui agents, directory — including `.github_pat`), and logs live under
`/opt/github-runners/runner-N/_diag/`.

## Running the supervisor manually

Useful for debugging a single runner without launchd:

```sh
GITHUB_PAT=ghp_... runner-ephemeral \
  --dir /opt/github-runners/runner-1 --org acme \
  --labels macos,macmini,self-hosted
```

`runner-ephemeral` never uses sudo and refuses to run as root (like every
runner process here, it runs as the invoking non-root user; we never set
`RUNNER_ALLOW_RUNASROOT`). Ctrl-C forwards the signal to the runner child and
shuts down cleanly. Options: `--name`, `--pat-file`, `--api-url` (GitHub
Enterprise), `--max-failures`, `--sandbox`, `--sandbox-deny-network`,
`--sandbox-cache-path`, `--snapshot-rollback` — see `runner-ephemeral --help`.

## Opt-in hardening

Both options are off by default; an unmodified invocation behaves exactly as
before. Full VM/image isolation and snapshot rollback of the *host* remain out
of scope (see the capability review in
[runner-performance.md](runner-performance.md)) — these are host-level
seatbelts, not disposable-machine guarantees.

### Sandboxed jobs (`--sandbox`)

With `--sandbox`, the per-cycle runner service (`bin/runsvc.sh`, which
supervises the job worker) is exec'd through `sandbox-exec` with a profile
generated at runtime. The profile is default-allow except for filesystem
writes, which are confined to:

- the runner's own directory tree (`RUNNER_DIR`),
- `/private/tmp`, `/private/var/tmp`, `/private/var/folders` (user temp dirs),
- the usual `/dev` essentials (`null`, `zero`, `random`, `urandom`, ttys),
- each `--sandbox-cache-path DIR` (repeatable) — designate shared writable
  caches explicitly, e.g. `--sandbox-cache-path /opt/github-runners/cache`.

Everything else on the host — the user's home, `/usr/local`, other runners'
directories — becomes read-only to the job. If `sandbox-exec` is unavailable
or the profile cannot be written, the cycle fails and backs off rather than
running unconfined.

Caveats:

- `sandbox-exec` is **deprecated but still shipped** with macOS. Treat the
  profile as a best-effort seatbelt against accidental or lazy escapes, not a
  security boundary.
- `--sandbox-deny-network` adds `(deny network*)` to the profile. Because the
  sandbox wraps the *entire* service tree, this also blocks the runner
  listener's own connection to GitHub — so it is only useful for fully
  offline/manual setups. For real network egress policy,
  [runner-netisolate](network-isolation.md)'s pf anchor (per-user, GitHub
  endpoint allowlist, VPN kill-switch) remains the stronger and supported
  control.

### Workspace snapshot rollback (`--snapshot-rollback`)

When `RUNNER_DIR` sits on an APFS volume, `--snapshot-rollback` replaces the
between-jobs `rm -rf _work` with a restore from a local APFS snapshot:

1. Right after each successful `config.sh` registration, the supervisor takes
   a fresh baseline with `tmutil localsnapshot` (the previous baseline is
   deleted; only one is kept).
2. At the start of the next cycle, the snapshot is mounted read-only
   (`mount_apfs -s`) and `_work` is restored from it with
   `rsync -a --delete`, so the workspace returns bit-for-bit to its
   post-registration state — including removal of anything a job left behind.
3. Registration residue (`.runner`/`.credentials*`) is still removed with
   `rm`, since those files post-date the baseline by design.

Any failure — non-APFS volume, `tmutil` failing, mount or rsync error — logs a
warning and falls back to the existing `rm -rf` cleanup for that cycle. A
snapshot failure **never** skips cleanup, and the exponential-backoff and
`--max-failures` semantics are unchanged.

`tmutil localsnapshot`, `mount_apfs`, and `umount` require root. The
supervisor tries each command directly and then via `sudo -n`
(non-interactive), so on a daemon host you need NOPASSWD sudoers entries for
those verbs for the runner user — otherwise every cycle warns and uses the
rm-based cleanup. Local snapshots that leak (e.g. after a hard kill) are
purged by Time Machine automatically within ~24 hours. This rolls back only
the runner's workspace subtree; it is not a host snapshot mechanism.

## Notes and caveats

- Ephemeral re-registration adds a few seconds of latency before each job
  (token fetch + `config.sh`). Throughput-sensitive farms should size N
  accordingly. The persistent [tool cache](#persistent-tool-cache) removes the
  much larger `setup-*` re-download cost from every job after the first.
- `_work` is wiped between cycles; job workspaces do not persist. `_diag`
  logs intentionally persist so failures remain diagnosable, and the
  per-runner `_toolcache` persists so installed tool versions survive resets.
  With `--snapshot-rollback` on APFS, the wipe is a snapshot restore with the
  same outcome (see above).
- A `--url` scope with a path deeper than `OWNER/REPO` cannot be mapped to an
  API endpoint — use `--org`/`--repo` (and `--api-url` for GHE) in that case.

The supervisor retains `.env` and `.path` so configured job hooks and tool
paths survive re-registration. `runner-setup` passes the runner name explicitly,
and manually launched supervisors pin their initial identity in memory, so
GitHub deleting `.runner` does not change the next registration's host prefix.

At supervisor startup, the standard Homebrew binary directories are appended
to PATH so older installed LaunchDaemon plists can find the formula-provided
`jq` dependency before `.path` is loaded. Existing PATH entries keep priority.
