# Runner performance and Blacksmith capability review

Reviewed against Blacksmith and upstream documentation on 2026-09-05.
This tap operates self-hosted macOS runners. Blacksmith is a useful benchmark
for fast startup, warm storage, workload sizing, and job visibility. Its
[runner catalog](https://docs.blacksmith.sh/blacksmith-runners/overview)
also includes Apple Silicon macOS runners; the comparison is not Linux-only.
No representative CI workload has been benchmarked on this checkout's host,
so this review does not establish speedup numbers or optimal fleet sizing.
The `runner-bench` harness (below) automates the benchmark procedure; the
acceptance table stays `pending` until a staging Mac runs it.

## Fixes from this review

- **Keep caches warm.** `runner-prune` now scopes post-job cleanup to the
  current runner and preserves package/tool/action caches. Shared eviction
  requires an explicit offline maintenance window. Previously a single hook
  could delete another runner's active checkout and all shared package caches.
- **Use the right cache protocol.** MinIO stores S3 objects. It does not
  implement GitHub's Actions cache/results APIs. `runner-cache env` now
  describes an explicit S3-aware action integration. Existing `ACTIONS_*`
  overrides pointing to MinIO must be removed; follow the
  [cache migration instructions](cache-backend.md).
- **Persist Docker build state.** Remote Docker-over-SSH uses a
  `docker-container` builder. Colima has an explicit endpoint, new eligible
  Apple Silicon profiles use VZ/VirtioFS, and setup refuses to discard an
  existing builder's cache to fix a configuration mismatch. Workflows select
  the persistent builder explicitly. See [Docker builds](docker-builds.md).
- **Observe actual queued jobs.** The autoscaler queries supported repository
  run/job endpoints, accounts for queued matrix jobs in running workflows,
  filters labels, paginates, and uses exact local runner names for idleness.
  A single matching queued job can grow a zero-sized fleet. Unknown state
  prevents scale-down. Org fleets specify the repositories they watch.
  See [autoscaling](autoscaling.md).
- **Reduce provisioning work.** Setup skips release queries/downloads when
  all requested binaries exist. New provisioning reuses archives from
  `~/Library/Caches/github-actions-runner`, verifying the published SHA-256
  before every reuse. Old cached versions can be removed during maintenance;
  automatic runner updates and `runner-upgrade` remain separate mechanisms.
- **Keep lifecycle configuration stable.** Ephemeral cycles preserve the
  configured runner name, `.env`, and `.path`; failures in cleanup or the
  runner service trigger backoff. New runner plists include Homebrew in PATH
  for headless supervisor startup. See [ephemeral runners](ephemeral-runners.md).

These changes are included in tooling release **v1.8.0**, along with the
[startup and rollback fixes](auto-upgrades.md) for `runner-upgrade`. Install
with `brew update` followed by `brew upgrade joeblau/hb/runner-setup`. Updating
the helper scripts does not itself upgrade GitHub's runner binaries or rewrite
existing service configuration; perform those steps after draining active jobs.

## Capability map

| Capability | This repository | Remaining work for comparable managed behavior |
| --- | --- | --- |
| Fast native compute | macOS arm64/x64 binaries and non-root LaunchDaemons | Benchmark actual Mac hardware; add capacity when CPU/RAM are saturated. No per-job CPU/RAM reservation or hardware provisioning. |
| Warm dependency cache | Persistent package caches; optional scoped S3 store with explicit workflow integration; optional GitHub Actions cache-protocol gateway (`runner-cache gateway`) with per-scope tokens and key-prefix save policy | Distributed cache service operation is not implemented. Branch authorization is a key-prefix naming policy only — the protocol carries no ref and the version hash is opaque. |
| Persistent Docker layers/cache mounts | Named BuildKit containers on remote Linux or Colima; managed BuildKit GC policies and a local registry pull-through mirror (`runner-docker-builder gc` / `registry-mirror`) | Provision/monitor builder hardware and tune GC thresholds for actual workloads; remote SSH daemons get mirror instructions, not automatic wiring. |
| Native multi-architecture builds | Explicit platforms supported; guide covers native Buildx nodes | Additional native hosts and node scheduling must be provided; labels do not add hardware or emulation. |
| Fast startup and capacity | Verified archive reuse, local min/max controller, one-job supervisor | No instant VM provisioning, cross-host scheduler, or guaranteed atomic drain. |
| Clean per-job environment | Workspace/registration reset; opt-in sandbox-exec write confinement and APFS snapshot workspace rollback on ephemeral runners | No disposable macOS image or full filesystem/process isolation; sandbox-exec is a seatbelt, not a security boundary. |
| Sticky storage | Local caches persist between jobs on a trusted host | No isolated per-job clones, branch-aware commit rules, or shared snapshot service. |
| Container and Git checkout acceleration | Persistent Docker worker, local registry pull-through mirror, and local bare Git mirrors with per-runner checkout rewiring (`runner-git-mirror`) | No container-init snapshotting or shared image-pull proxy beyond the local registry mirror. |
| Bazel/other compiler caches | Per-scope Bazel remote cache service (`runner-bazel-cache`); ccache/sccache setup guidance | No automatic ccache/sccache configuration — that stays workflow-owned. |
| Logs and health | JSON-lines logs/metrics, watchdog, disk monitoring, token and upgrade tools; step-level CPU/memory timelines, JUnit ingestion with flake history, and a static HTML dashboard (`runner-logs steps` / `tests` / `dashboard`) | No network timeline, per-step network attribution, or interactive testbox. |
| Agent-driven CI fixes (Codesmith) | `runner-agent` + reusable workflow: Claude Code runs on the runner itself, fixes failed runs into PRs and acts on `@runner-agent` mentions, with `runner-mcp` for host diagnostics and optional Slack notifications ([docs](runner-agent.md), [agent-tools.md](agent-tools.md)) | No cross-repository changes or hosted queue; Slack is notification-only, and infrastructure remediation is recommended, not applied, and stays with the operator's `runner-*` commands. |
| Networking | Existing pf/VPN policy helper | Private routing, DNS, registry allowlists, and host isolation need deployment-specific validation. |

Blacksmith documents separate systems for
[sticky disk snapshots](https://docs.blacksmith.sh/blacksmith-caching/dependencies-sticky-disks),
[container initialization](https://docs.blacksmith.sh/blacksmith-caching/docker-container-caching),
[Git checkout caching](https://docs.blacksmith.sh/blacksmith-caching/git-checkout-caching),
and [Bazel caching](https://docs.blacksmith.sh/blacksmith-caching/bazel-build-caching).
Keeping a writable directory on one Mac does not reproduce those services.
Its [metrics](https://docs.blacksmith.sh/blacksmith-observability/metrics)
and [test analytics](https://docs.blacksmith.sh/blacksmith-observability/test-analytics)
are additional capabilities beyond the local log/health tooling.

## Choose concurrency from measurements

Start with one representative job, then repeat at two and four concurrent
jobs as the machine permits. Measure full workflow completion and throughput,
not just how quickly runners register. More runner processes share the same
CPU, memory, disk, package caches, and any Colima VM; they do not create
capacity. A parallel compiler in every job can oversubscribe the machine.

Use these read-only commands to capture the host context:

```sh
sysctl -n hw.model hw.ncpu hw.memsize
sysctl vm.swapusage
df -h /opt/github-runners
runner-docker-builder status
runner-logs metrics --once
```

As a starting estimate, bound runner count by both the available CPU divided
by each job's useful parallelism and available RAM divided by observed peak
job memory. Reserve resources for macOS, cache services, and Colima. This is
a sizing heuristic, not an enforced limit. Use workflow/tool-specific worker
limits for tests, Cargo, Gradle, and Xcode after measuring their scaling.

Prefer native architecture for compilation. Place remote builders near the
runners and registries. Use lockfile, OS, architecture, and compiler-version
cache keys; separate writable caches by project/trust boundary. Avoid saving
huge directories if compression and transfer cost more than recreating them.

## Benchmark harness (runner-bench)

`runner-bench` runs one pinned, representative workload repeatedly under
controlled cache scenarios, samples resources while it runs, appends one JSON
record per workload instance (plus one host-wide record per repetition) to a
raw results file, and renders median/p95 comparison summaries. It runs as the
invoking user: no sudo, no `/opt`, no launchd. Cold state comes from a
disposable cache directory inside the benchmark workdir — production caches
(`~/Library/Caches`, runner caches, BuildKit state) are never purged to
obtain a baseline.

### Pin the workload

The `--cmd` workload is the benchmark. Pin everything inside it: source
revision (clone a tagged ref or a fixed commit from a local mirror), toolchain
(record it with `--toolchain`, e.g. `'rustc 1.80.1, Xcode 26.0, arm64'`),
and target architecture (the recorded host block carries `arch` and
`hw.model`). Keep the command, `--results-dir`, and the machine's resource
limits identical between baseline and candidate runs — `report` warns when
the compared groups recorded different commands or hosts.

```sh
BENCH='git clone -q file:///opt/mirrors/monorepo.git src && cd src &&
  git checkout -q v2026.09.0 &&
  bench_phase() { printf "RUNNER_BENCH_PHASE %s %s\n" "$1" "$(date +%s)"; } &&
  bench_phase checkout &&
  ./ci/restore-deps.sh && bench_phase restore &&
  ./ci/build.sh        && bench_phase build &&
  ./ci/test.sh         && bench_phase test &&
  ./ci/save-cache.sh   && bench_phase save'
```

### Measure each scenario

```sh
runner-bench run --scenario cold   --label baseline --reps 5 --cmd "$BENCH" \
  --toolchain 'rustc 1.80.1, arm64'
runner-bench run --scenario warm   --label baseline --reps 5 --cmd "$BENCH"
runner-bench run --scenario edited --label baseline --reps 5 --cmd "$BENCH"
runner-bench run --scenario warm   --label baseline --reps 5 --concurrency 2 \
  --cmd "$BENCH"
# repeat all of the above with --label candidate after changing configuration
runner-bench report --baseline baseline --candidate candidate
```

- `cold` wipes only `--cache-dir` (default `<workdir>/.bench-cache`, exposed
  to the workload as `RUNNER_BENCH_CACHE_DIR`) before each repetition and
  refuses any cache dir outside the workdir. Point the workload's dependency
  caches at it.
- `warm` runs unchanged; `edited` appends one small deterministic line to
  `--edit-file` per repetition to simulate a small source edit.
- `--concurrency N` runs N identical copies in `WORKDIR/slot-N`. Per-job
  records (`scope: "job"`, one per slot) stay separate from the host-wide
  record (`scope: "host"`), so overlapping jobs are distinguishable.

### What is recorded

Every record carries the host block (model, CPU count, RAM, arch, macOS,
machine label), label/scenario/concurrency/repetition, queue delay (when
`--queued-at` or `RUNNER_BENCH_QUEUED_AT` supplies the queue timestamp — take
it from the GitHub API's run/job `created_at` on a real runner), full
duration, per-phase durations from `RUNNER_BENCH_PHASE` markers, peak/mean
CPU and peak RSS from process-tree samples, host swap growth and disk-usage
growth, cache hit/miss/restore/save counters the workload writes to
`RUNNER_BENCH_STATS_FILE`, and disposable-cache growth. Measurements that
cannot be collected (queue delay without `--queued-at`, disk I/O rates,
absent cache counters) are named in each record's `unsupported` array and
shown as `unavailable` in reports — never invented.

Raw results, samples, and workload logs are retained under `--results-dir`
(`runner-bench.jsonl`, `samples/`, `logs/`). A failed workload is recorded
with its exit code and the run continues (final exit 1); interrupting
(SIGINT/SIGTERM) kills the whole workload process trees, records `cancelled`
results, and exits 130 — no orphan processes.

`report` groups by (label, scenario, concurrency), prints median/p95/min/max
with the sample size stated, warns below five repetitions, and with
`--baseline`/`--candidate` prints measured deltas plus `REGRESSION:` flags
when failures, cancellations, swap growth, or cache growth increase. No
speedup is claimed where either side lacks measurements.

For real CI jobs (as opposed to this synthetic harness), `runner-logs steps`
already emits per-step CPU/RSS timelines and `runner-logs jobs`/ship mode
emit job durations and results from the same fleet, so harness numbers and
production numbers can be compared in one aggregator.

## Benchmark and acceptance procedure

1. Pin source revision, OS/toolchain versions, target architecture, test data,
   and output destination. Capture current full-job and queue times before
   switching configuration (`runner-bench run --label baseline ...`; queue
   delay via `--queued-at`).
2. Measure a cold cache using disposable test state, an unchanged warm run,
   and a warm run with a small source edit (`--scenario cold|warm|edited`).
   Repeat each at least five times (`--reps 5`); keep production cache
   contents intact — `runner-bench` only ever wipes the disposable cache dir
   inside its workdir.
3. Record checkout, dependency restore/install, compilation, tests, cache
   save, artifact upload, queue delay, disk growth, and peak RAM/swap —
   `RUNNER_BENCH_PHASE` markers cover the phases; the sampler covers RAM,
   swap, CPU, and disk growth. Include multiple jobs at the chosen fleet
   limit (`--concurrency N`).
4. Exercise unavailable cache/builder/API, a cancelled job, supervisor
   restart, and a reboot on a staging Mac. A cancelled `runner-bench` run
   must record `cancelled` results and leave no orphan processes; confirm
   useful failure reporting, retained logs, and that cleanup never affects a
   neighboring active job.
5. Compare medians and tail latency (`runner-bench report --baseline ...
   --candidate ...`). Adopt the configuration that improves end-to-end
   results without increasing failures, swapping, or unbounded cache growth —
   the report flags exactly those regressions. Fill the table with measured
   values before claiming parity.

| Scenario | Queue wait | Full job median | Full job p95 | Jobs/hour | Peak RAM/swap | Cache restore/save |
| --- | --- | --- | --- | --- | --- | --- |
| Current deployment | pending | pending | pending | pending | pending | pending |
| Revised tooling, cold | pending | pending | pending | pending | pending | pending |
| Revised tooling, warm | pending | pending | pending | pending | pending | pending |
| Revised tooling, warm + concurrent | pending | pending | pending | pending | pending | pending |

## Local regression checks

```sh
python3 -m unittest discover -s tests -p 'test_runner_*.py' -v
for tool in runner-*; do /bin/bash -n "$tool"; done
ruby -c Formula/runner-setup.rb
git diff --check
```

The tests use temporary files and mocked commands/APIs. They check protocol
configuration, builder selection/cache preservation, queue decisions, cleanup
boundaries, and lifecycle failures without installing software, creating
runners, publishing images, or changing launchd. They do not replace the
staging-Mac lifecycle checks or real workload measurements above.
