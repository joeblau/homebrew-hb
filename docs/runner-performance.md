# Runner performance and Blacksmith capability review

Reviewed against Blacksmith and upstream documentation on 2026-09-05.
This tap operates self-hosted macOS runners. Blacksmith is a useful benchmark
for fast startup, warm storage, workload sizing, and job visibility. Its
[runner catalog](https://docs.blacksmith.sh/blacksmith-runners/overview)
also includes Apple Silicon macOS runners; the comparison is not Linux-only.
No representative CI workload has been benchmarked on this checkout's host,
so this review does not establish speedup numbers or optimal fleet sizing.

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

These changes are in the working source. The Homebrew formula's stable URL
still points to the published v1.7.0 archive. Shipping them through `brew
upgrade` requires a new release and its actual archive checksum. Existing
installed services do not change just because files in this checkout change;
apply configuration changes during a drained maintenance window.

## Capability map

| Capability | This repository | Remaining work for comparable managed behavior |
| --- | --- | --- |
| Fast native compute | macOS arm64/x64 binaries and non-root LaunchDaemons | Benchmark actual Mac hardware; add capacity when CPU/RAM are saturated. No per-job CPU/RAM reservation or hardware provisioning. |
| Warm dependency cache | Persistent package caches; optional scoped S3 store with explicit workflow integration | GitHub-protocol cache gateway, branch authorization, and distributed service operation are not implemented. |
| Persistent Docker layers/cache mounts | Named BuildKit containers on remote Linux or Colima; registry export examples | Provision/monitor builder hardware, tune garbage collection, and configure registry mirrors for actual workloads. |
| Native multi-architecture builds | Explicit platforms supported; guide covers native Buildx nodes | Additional native hosts and node scheduling must be provided; labels do not add hardware or emulation. |
| Fast startup and capacity | Verified archive reuse, local min/max controller, one-job supervisor | No instant VM provisioning, cross-host scheduler, or guaranteed atomic drain. |
| Clean per-job environment | Workspace/registration reset | No disposable macOS image, filesystem/process isolation, or snapshot rollback of the host. |
| Sticky storage | Local caches persist between jobs on a trusted host | No isolated per-job clones, branch-aware commit rules, or shared snapshot service. |
| Container and Git checkout acceleration | Persistent Docker worker and ordinary checkout commands | No dedicated image-pull proxy, container-init snapshotting, or safe Git mirror service. |
| Bazel/other compiler caches | Can use workflow-configured native tool caches | No Bazel remote cache service or automatic ccache/sccache configuration. |
| Logs and health | JSON-lines logs/metrics, watchdog, disk monitoring, token and upgrade tools | No integrated step-level CPU/memory/network timeline, JUnit ingestion, flake history, dashboard, or interactive testbox. |
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

## Benchmark and acceptance procedure

1. Pin source revision, OS/toolchain versions, target architecture, test data,
   and output destination. Capture current full-job and queue times before
   switching configuration.
2. Measure a cold cache using disposable test state, an unchanged warm run,
   and a warm run with a small source edit. Repeat each at least five times;
   keep production cache contents intact.
3. Record checkout, dependency restore/install, compilation, tests, cache
   save, artifact upload, queue delay, disk growth, and peak RAM/swap. Include
   multiple jobs at the chosen fleet limit.
4. Exercise unavailable cache/builder/API, a cancelled job, supervisor
   restart, and a reboot on a staging Mac. Confirm useful failure reporting,
   retained logs, and that cleanup never affects a neighboring active job.
5. Compare medians and tail latency. Adopt the configuration that improves
   end-to-end results without increasing failures, swapping, or unbounded
   cache growth. Fill the table with measured values before claiming parity.

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
