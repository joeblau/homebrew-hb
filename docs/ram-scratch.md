# RAM-backed build scratch (opt-in)

Depot advertises RAM-backed disk acceleration on macOS. This tap implements a
deliberately narrower version: a **bounded RAM-backed scratch volume for
explicitly selected temporary/build directories**, not a transparent root-disk
accelerator. Depot's published accelerator also buffers root-disk I/O; this
feature does not reproduce that implementation or its advertised speedup.

The feature is **off by default** and stays opt-in unless measured staging
results (see [benchmarking](#benchmarking-ssd-vs-ram)) justify a different
default.

## Architecture

`runner-ramscratch` manages **one host-wide RAM volume** (created with
`hdiutil attach -nomount ram://` + `newfs_hfs`, mounted at
`/opt/github-runners/_ramscratch/volume`) owned by the runner user — no sudo,
and it refuses to run as root. Every job gets its own subdirectory
(`volume/<runner>/<job>`); two simultaneous jobs on different runners get
separate directories inside the one bounded volume.

- **Explicit sizing, host-wide budget.** Nothing works until
  `runner-ramscratch config --total-budget-mb N` (or
  `runner-setup --ram-scratch-budget-mb N`) writes the config. An allocation
  ledger (guarded by an inter-runner mkdir lock) refuses any request that
  would push the sum of all runners' allocations past the budget, and the
  volume is created no larger than the budget — and never larger than physical
  RAM.
- **Memory headroom.** Before creating the volume or granting an allocation,
  `vm_stat` free+inactive memory must exceed the request plus a configurable
  floor (`--min-free-mb`, default 4096 MB). Otherwise the allocation fails and
  the job runs on SSD instead.
- **Ownership.** The mounted volume is verified to be owned by and writable by
  the invoking runner user; a root-owned or unwritable mount is detached and
  rejected.

```
runner-ramscratch config --total-budget-mb 4096 --min-free-mb 4096
runner-ramscratch alloc --runner runner-1 --job cycle-3 --size-mb 1024
#   -> /opt/github-runners/_ramscratch/volume/runner-1/cycle-3 (stdout)
runner-ramscratch inspect
runner-ramscratch clean --runner runner-1 --job cycle-3
runner-ramscratch clean --all --offline   # full teardown; mirrors runner-prune
```

Exit codes: `0` ok, `1` failure, `2` usage, `3` not configured/unavailable,
`4` budget exhausted or insufficient memory. `3` and `4` both mean "fall back
to SSD".

## Runner integration (runner-setup + runner-ephemeral)

```sh
GITHUB_PAT=ghp_... runner-setup --org acme --token AYZ... --runners 2 \
  --ephemeral --ram-scratch --ram-scratch-budget-mb 4096 --ram-scratch-job-mb 2048
```

`--ram-scratch` requires `--ephemeral` (the per-job lifecycle hooks live in
the supervisor) and explicit sizes: a host-wide budget and a per-runner
per-job share. Keep `job-mb x --runners <= budget-mb`. Setup writes the config
to `/opt/github-runners/_ramscratch/config` (mode 600, runner user), passes
`--ram-scratch --ram-scratch-mb N` to the supervisor's `ProgramArguments`, and
sets `RUNNER_RAMSCRATCH=1` in the plist's `EnvironmentVariables` so older
argument sets still see the toggle.

Each supervisor cycle then:

1. **Before the job**, allocates `RUNNER_RAM_SCRATCH` via
   `runner-ramscratch alloc` and points job steps at it — exported in the
   service environment **and** written into the runner's `.env` (other `.env`
   lines, including configured job hooks, are preserved line-for-line; only
   the `RUNNER_RAM_SCRATCH=` line is replaced, and it is removed again at
   teardown). Existing hooks such as
   `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` are never overwritten.
2. **On allocation failure** (feature unconfigured, helper missing, budget
   exhausted, low memory), falls back to `RUNNER_DIR/_work/_scratch` on SSD —
   the job still runs with a working `RUNNER_RAM_SCRATCH` path. When the
   feature is not enabled at all, nothing is set and workflows should default
   with `${RUNNER_RAM_SCRATCH:-$RUNNER_TEMP}`.
3. **After the job** (or on cancellation via TERM/INT), copies the job's
   designated `_out` subdirectory to retained SSD storage under
   `_diag/ramscratch/<job>-<timestamp>/` **before** releasing the allocation,
   so artifacts and logs survive the volatile volume — including for
   interrupted jobs. `runner-ramscratch clean` then removes only this job's
   directory and detaches the shared volume only once no job holds an
   allocation.

With `--sandbox`, a RAM scratch path outside the runner tree is automatically
added to the writable paths of the generated `sandbox-exec` profile.

## What belongs on the RAM scratch — and what stays on SSD

Only explicitly selected **temporary/build** directories. Workflows opt in by
pointing supported tools at subdirectories of `$RUNNER_RAM_SCRATCH`, e.g.:

```yaml
env:
  DERIVED_DATA_PATH: ${{ env.RUNNER_RAM_SCRATCH }}/DerivedData   # xcodebuild
  TMPDIR: ${{ env.RUNNER_RAM_SCRATCH }}/tmp                      # if safe for the job
steps:
  - run: cmake -B "$RUNNER_RAM_SCRATCH/build" -S .
  - run: cmake --build "$RUNNER_RAM_SCRATCH/build"
  - run: cp build/results.zip "$RUNNER_RAM_SCRATCH/_out/"        # survives teardown
```

Always on SSD, never on the RAM volume:

- installed tools — the persistent per-runner `_toolcache`
  (`AGENT_TOOLSDIRECTORY` / `RUNNER_TOOL_CACHE`, see
  [ephemeral-runners.md](ephemeral-runners.md#persistent-tool-cache));
- compiler/dependency caches — sccache ([compiler-cache.md](compiler-cache.md)),
  the Bazel remote cache ([bazel-cache.md](bazel-cache.md)), and the S3 cache
  backend: caches are only useful because they persist, and the RAM volume is
  volatile by design;
- credentials and registration state;
- retained logs — `_diag` (the scratch `_out` copy-out destination).

## Failure policy

- **Pre-job**: any allocation problem → SSD fallback; the job runs. There is
  no silent downgrade mid-flight.
- **Mid-job exhaustion**: a job that outgrows its share fails the affected
  build step with ENOSPC on the bounded RAM volume (host disk is unaffected).
  The job is **not** retried and steps are **not** migrated or silently rerun
  — non-idempotent steps stay safe. Size `--ram-scratch-job-mb` from measured
  peak usage.
- **Unmount safety**: `clean` detaches the volume only when the allocation
  ledger is empty, and `hdiutil detach` itself refuses while a consumer holds
  files open — that failure keeps the volume mounted and warns, so a later
  clean retries. `clean` validates runner/job names, refuses symlinked
  targets/parents, and never removes a path outside the mount, so one job's
  cleanup cannot delete another job's workspace, unmount an active volume, or
  touch persistent caches (which never live on the volume anyway).
- **Reboot / stale state**: RAM volumes do not survive a reboot, but the
  ledger under `/opt/github-runners/_ramscratch` does. The next
  `alloc`/`inspect`/`clean` detects that the recorded device is no longer
  mounted at the mount point, warns, and resets the stale state before
  proceeding. A crashed holder of the inter-runner lock is detected via its
  recorded PID and the lock is retaken.
- **Teardown ordering**: artifacts (`_out`) are copied out before teardown;
  teardown happens after the runner service exits, and `runner-cleanup --all`
  unmounts the shared volume (via `clean --all --offline`) only after every
  runner daemon — the consumers — has been booted out. Removing a single
  runner leaves the shared volume alone.

`runner-prune` needs no RAM scratch rules: it scopes job-hook cleanup to
`_work`, which never contains the RAM volume, and its symlink/mount safeguards
are unchanged — scratch directories are real directories on a separately
mounted volume, not symlinks inside `_work`.

## Benchmarking SSD vs RAM

Use the [`runner-bench`](runner-performance.md#benchmark-harness-runner-bench)
harness from the same pinned workload twice — once with build/temp dirs on
SSD, once on a `runner-ramscratch` allocation:

```sh
# Baseline: workload builds on SSD (default paths).
runner-bench run --scenario warm --label ssd --reps 5 --cmd "$BENCH_SSD" \
  --toolchain 'Xcode 26.0, arm64'

# Candidate: the same pinned workload with build/temp dirs redirected onto
# a RAM scratch allocation of the intended production size.
scratch="$(runner-ramscratch alloc --runner bench --job ram1 --size-mb 4096)"
runner-bench run --scenario warm --label ram --reps 5 --cmd "$BENCH_RAM"
runner-ramscratch clean --runner bench --job ram1

runner-bench report --baseline ssd --candidate ram
```

The harness records per-phase durations (`RUNNER_BENCH_PHASE` markers), peak
RSS, **host swap growth**, and disk-usage growth per run, and `report` flags
swap/cache-growth regressions — exactly the memory/swap impact a RAM volume
can cause. Complement it with `sysctl vm.swapusage` and
`runner-ramscratch inspect` before/after, and repeat at production
concurrency (`--concurrency N`) since concurrent jobs share one bounded
budget. Adopt (or widen the rollout of) RAM scratch only if medians improve
without increased failures or swap growth; keep it opt-in otherwise.
