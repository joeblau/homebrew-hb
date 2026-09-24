# Fast Docker image builds on macOS runners

`runner-docker-builder` provisions a persistent BuildKit builder on a remote
Linux Docker host or in a local Colima VM. Keeping its layers and dependency
cache mounts warm avoids repeated downloads and compilation. Actual speed
also depends on CPU, storage, architecture, network distance, and workload;
this repository has no measured speedup against Docker Desktop or Blacksmith.

Run setup once as the runner's service user, between jobs. Use the same
`HOME` and `DOCKER_CONFIG` in setup and workflows. The helper installs missing
Docker tooling with Homebrew and links its Buildx plugin into
`${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins` when necessary, following the
[Colima installation instructions](https://colima.run/docs/installation/).

## Remote Linux builder

The remote host needs a running Linux Docker daemon and noninteractive SSH
access from the runner user. Configure the host key and an SSH key or agent
available to that service account; interactive shell access alone does not
establish access for a LaunchDaemon.

```sh
runner-docker-builder setup-remote --host ssh://ci@linux-builder.internal
# Docker also handles SSH ports and SSH config aliases:
runner-docker-builder setup-remote --host ssh://ci@linux-builder.internal:2222
```

The helper verifies the remote Docker daemon, creates `runner-remote` with
the `docker-container` driver at that exact endpoint, boots it, and selects
it for Buildx. A matching existing builder and its volume are reused. The
`remote` driver is for an independently managed **BuildKit daemon**, not an
ordinary Docker daemon accessed over SSH. See Docker's
[driver reference](https://docs.docker.com/build/builders/drivers/remote/) and
[builder creation reference](https://docs.docker.com/reference/cli/docker/buildx/create/).

Prefer a nearby host with fast SSD storage and the CPU architecture of your
build target. A cloud VM is supported; the helper does not turn it into
bare metal or eliminate that host's virtualization overhead.

## Local Colima builder

```sh
runner-docker-builder setup-colima                  # 4 CPUs, 8 GiB RAM, 60 GiB disk
runner-docker-builder setup-colima --cpu 6 --mem 12 --disk 100
```

This starts Colima's `default` profile using Docker and binds
`runner-colima` explicitly to the `colima` Docker context, even if Desktop
or another context was previously active. Newly created Apple Silicon
profiles on macOS 13+ use Apple's VZ framework and VirtioFS mounts. Existing
profiles keep their VM and mount type. See
[Colima configuration](https://colima.run/docs/configuration/).

### linux/amd64 runs through Rosetta, never QEMU

On Apple Silicon, `linux/amd64` images run under emulation inside the VM.
Colima's default (`binfmt: true`) registers QEMU user-mode handlers, and gcc
under QEMU segfaults on large C builds. A typical failure is
`internal compiler error: Segmentation fault signal terminated program cc1`
while building `aws-lc-sys`, then `exit status: 4`. Rosetta for Linux runs
the same builds reliably and much faster.

`setup-colima` therefore:

- creates new VZ profiles with `--vz-rosetta --binfmt=false`;
- rewrites an existing VZ profile's `colima.yaml` to `rosetta: true` and
  `binfmt: false` (a running VM picks this up on its next start);
- removes the live `qemu-x86_64`/`qemu-i386` handlers from the running VM.

`wait-colima`, which gates every runner started by `runner-setup --colima`,
repeats the removal on every runner start. The runner stays offline if QEMU
can't be removed or Rosetta isn't active, so an amd64 job never falls back to
QEMU. It also stays offline on a non-VZ (QEMU-engine) profile, which cannot
use Rosetta. To move an existing runner host to Rosetta, drain its jobs, then:

```sh
runner-docker-builder setup-colima                        # writes colima.yaml
sudo launchctl kickstart -k system/com.github.runner-colima   # restart the VM
colima ssh -- ls /proc/sys/fs/binfmt_misc                 # rosetta, no qemu-*
```

Jobs that run `docker/setup-qemu-action` or `tonistiigi/binfmt --install`
re-register QEMU until the next runner start; remove those steps from Mac
workflows. `RUNNER_ALLOW_QEMU=1` in the environment of setup and the runner
services opts a host out. Apple has said full Rosetta support ends after
macOS 27; for a durable fix, cross-compile to x86_64 on a native arm64 build
stage instead of emulating.

An already running VM keeps its current CPU/memory/disk allocation. To resize,
stop Colima between jobs and rerun setup with the desired resource flags.
Leave enough host RAM and CPU for concurrent native macOS jobs. Colima disk
size can increase; shrinking an existing disk is unsupported.

```sh
runner-docker-builder status
runner-docker-builder status --json   # for scripts and agents (docs/agent-tools.md)
runner-docker-builder use-desktop
```

`use-desktop` verifies and selects Docker Desktop's `desktop-linux` context
and builder. It reports an error if Desktop is unavailable.

### Start Docker and runners together at every boot

Add `--colima` to `runner-setup` on each CI Mac. Setup installs Colima and its
Docker/Buildx tooling, creates one system LaunchDaemon for the default VM, and
wraps each runner's service entrypoint with a Docker readiness gate:

```sh
# New persistent runners (RUNNER_TOKEN contains a registration token).
runner-setup --org acme --runners 2 --colima

# New ephemeral runners also need GITHUB_PAT for re-registration.
runner-setup --org acme --runners 2 --ephemeral --colima

# Existing registered runners: no new token is needed for a same-scope retrofit.
# Pause new jobs and allow current jobs to finish before changing services.
runner-setup --org acme --runners 2 --colima --docker-timeout 300
```

Use `--repo OWNER/REPO` instead of `--org` for repository-scoped runners.
The number selects runner-1 through runner-N. A new or differently scoped
runner still requires `RUNNER_TOKEN`; tokenless setup validates the entire
selection before provisioning anything.

At each boot, launchd starts `com.github.runner-colima` as the runner's
non-root service user, with explicit `HOME`, `COLIMA_HOME`, `DOCKER_CONFIG`,
and PATH. It runs `colima start default --foreground`, using the existing VM
configuration and keeping its image and build caches. Unlike a user-level
`brew services start colima`, this is a system service with `RunAtLoad` and
does not require a GUI login. [Colima foreground mode](https://github.com/abiosoft/colima/blob/main/docs/FAQ.md#does-colima-support-autostart)
is designed for service supervision; Homebrew documents the
[boot/login distinction](https://docs.brew.sh/Manpage#services-subcommand).

Each runner daemon starts through `runner-docker-builder wait-colima`.
The gate polls `docker --context colima info` with bounded probe timeouts,
then execs the original `bin/runsvc.sh` or `runner-ephemeral` command. Until
Docker responds, the runner listener does not start or accept jobs. This
also covers job/service containers, which are created before workflow steps.
A timeout leaves the runner offline and exits unsuccessfully, so launchd
retries. `--docker-timeout` accepts 1–3600 seconds (default 300).
The started runner inherits `DOCKER_CONTEXT=colima`; conflicting inherited
`DOCKER_HOST` and TLS overrides are removed by the gate.

All runners share one VM and daemon, so they never race to start separate
Colima instances. Once the host has opted in, later `runner-setup` calls,
including autoscaler provisioning, inherit its Colima configuration and
readiness timeout. Updating an existing runner preserves its registration,
credentials, supervisor arguments and existing maintenance hold. The update
uses `runner-upgrade` shutdown/start to check active jobs and verify readiness.
Runner upgrades and scope repairs preserve the readiness gate.

If a Homebrew Colima service is already registered, stop that service between
jobs before opting in (`brew services stop colima` for a user service, or
`sudo brew services stop colima` for a system service). Setup refuses competing
service managers and a shared daemon owned by another user. Configure VM
CPU/memory/disk beforehand with `runner-docker-builder setup-colima` if the
default 4 CPUs, 8 GiB memory and 60 GiB disk are unsuitable.

Inspect startup on each Mac with:

```sh
sudo launchctl print system/com.github.runner-colima
sudo launchctl print system/com.github.runner-1
docker --context colima info
tail -n 50 "$HOME/Library/Logs/runner-colima/stderr.log"
tail -n 50 /opt/github-runners/runner-1/_diag/runner-stderr.log
```

Check one canary Mac after a reboot before rolling out fleet-wide; fixture
tests do not validate your VM backend or host's pre-login environment.
FileVault's pre-boot unlock must complete before macOS services can start.
Colima readiness is a startup gate, not a monitor that interrupts jobs if
Docker becomes unavailable later.

## Explicit workflow selection

Use `--builder runner-remote` (or `runner-colima`) for predictable routing:

```sh
docker buildx build --builder runner-remote --push -t ghcr.io/OWNER/IMAGE:TAG .
# To import a single-platform image into the client's current Docker daemon:
docker buildx build --builder runner-remote --load -t app:test .
```

`--load` needs a reachable Docker daemon on the client side; `--push` exports
directly to the registry. Without either output, the result stays in the
builder's cache. Plain `docker build` uses the Docker Engine's default
builder unless you pass `--builder` or set `BUILDX_BUILDER`; selecting a
Buildx builder does not transparently reroute all Docker commands. See
[Docker's builder selection rules](https://docs.docker.com/build/builders/).

For a provisioned persistent builder, pass the
[`build-push-action` builder input](https://github.com/docker/build-push-action)
and omit `setup-buildx-action`. Its default behavior creates a separate
builder and removes it after the job. The example publishes trusted `main`
branch builds and serializes writers to the shared image/cache tags:

```yaml
name: build
on:
  push:
    branches: [main]
permissions:
  contents: read
  packages: write
concurrency:
  group: docker-main-${{ github.repository }}
  cancel-in-progress: false
jobs:
  docker:
    runs-on: [self-hosted, macOS]
    steps:
      - uses: actions/checkout@v6
      - uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v7
        with:
          builder: runner-remote
          context: .
          push: true
          tags: ghcr.io/${{ github.repository }}:latest
          cache-from: type=registry,ref=ghcr.io/${{ github.repository }}:buildcache-main
          cache-to: type=registry,ref=ghcr.io/${{ github.repository }}:buildcache-main,mode=max
          provenance: mode=min
          sbom: true
```

Use a lowercase registry path if your GitHub owner/repository contains
uppercase characters. SBOM generation adds work; measure its cost separately
from image compilation. These current action majors use Node 24; update the
self-hosted Actions runner to a compatible release before using them.

For a temporary, job-managed builder, use
[`setup-buildx-action`](https://github.com/docker/setup-buildx-action) with an
explicit Docker endpoint and pass its output to the build action:

```yaml
- uses: docker/setup-buildx-action@v4
  id: buildx
  with:
    driver: docker-container
    endpoint: ssh://ci@linux-builder.internal
- uses: docker/build-push-action@v7
  with:
    builder: ${{ steps.buildx.outputs.name }}
    context: .
    push: true
    tags: ghcr.io/${{ github.repository }}:latest
    cache-from: type=registry,ref=ghcr.io/${{ github.repository }}:buildcache-main
```

That job-managed option needs registry login and SSH authentication too.
Use the persistent helper for warm cache mounts across jobs. Do not run setup,
remove builders, or change global Docker configuration during concurrent jobs.

## Cache and architecture tuning

Registry cache is the supported shared-cache example. `mode=max` exports
intermediate stages as well as final-stage layers. Use separate writable
cache references for each image, branch, and architecture where appropriate,
and import both branch and main caches. Concurrent exporters to the same
reference overwrite it, so serialize writers or give them distinct refs.
[Docker cache backends](https://docs.docker.com/build/cache/backends/) documents
multiple imports, modes, and driver requirements.

Use a small `.dockerignore`, copy dependency manifests before frequently
changed source, and add `RUN --mount=type=cache` to expensive package/compiler
steps. Those cache mounts live on the BuildKit worker; registry layer export
does not make their contents portable across unrelated workers. Persistent
builder volumes and registry cache complement one another. See
[cache optimization](https://docs.docker.com/build/cache/optimize/) and
[BuildKit cache mounts](https://docs.docker.com/reference/dockerfile/#run---mounttypecache).

S3 is **not a supported default here**: Docker's backend overview marks it
unreleased and its dedicated page marks it experimental. Using it requires
verifying support in your exact BuildKit image and arranging credentials
where the BuildKit daemon runs; merely exporting AWS variables on the Mac
or deploying the companion Actions cache service does not configure a remote
BuildKit container. See the [S3 backend documentation](https://docs.docker.com/build/cache/backends/s3/).

For multi-platform images, prefer native nodes or Dockerfile cross-compilation
before emulation. `setup-remote --platform linux/amd64,linux/arm64` advertises
platforms at creation; it does not install QEMU or add native capacity. On an
existing builder that option leaves its platform configuration unchanged.
Check `docker buildx inspect --bootstrap runner-remote`, then select targets
with the build's `--platform` flag. For native clusters, manage a separate
multi-node builder using `docker buildx create --append`. See
[Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/).

BuildKit supports registry mirrors, garbage collection, and maximum
parallelism through a daemon configuration file. `runner-docker-builder`
manages GC policy and a loopback registry mirror for you — see the next two
sections. Settings apply at builder creation, so tune them for the builder's
actual CPU/RAM, disk capacity, and registry access before creating it; the
helper also honors Buildx's standard `buildkitd.default.toml` lookup. Keep
cache space bounded without pruning every job. See
[BuildKit configuration](https://docs.docker.com/build/buildkit/toml-configuration/).

## BuildKit garbage collection

```sh
runner-docker-builder gc status                            # docker buildx du for the selected builder
runner-docker-builder gc status --builder runner-remote --verbose
runner-docker-builder gc configure --builder runner-remote # write the default GC policy
runner-docker-builder gc configure --builder runner-remote --max-used-space 80 --keep-hours 72
```

`gc configure` writes a managed `buildkitd.toml` per builder under
`${DOCKER_CONFIG:-~/.docker}/buildkitd/` with these defaults (all overridable):

| Setting | Default | Meaning |
| --- | --- | --- |
| `reservedSpace` | 10GB (`--reserved-space`) | Always kept; GC never reclaims below it |
| `maxUsedSpace` | 50GB (`--max-used-space`) | Above this, GC reclaims aggressively |
| `keepBytes` | 20GB (`--keep-bytes`) | Per-rule retention for the default GC rules |
| `keepDuration` | 48h (`--keep-hours`, 0 disables) | Age after which cache mounts/sources expire |

The config carries two default rules: one expiring cache mounts and VCS/local
sources after `keepDuration`, and a catch-all `all = true` rule, each retaining
up to `keepBytes`. `reservedSpace`/`maxUsedSpace` require BuildKit v0.20+; the
`gcpolicy` rules are honored by every version and older daemons ignore unknown
keys.

GC configuration is **creation-time only**: the next `setup-remote` or
`setup-colima` passes the file to `docker buildx create --config`. If the
builder already exists, the helper refuses to touch it — the existing builder
and its cache are left unchanged, exactly like the setup mismatch guard. To
apply a new policy, schedule a maintenance window and recreate with the cache
volume preserved:

```sh
docker buildx rm --keep-state runner-remote   # keeps the BuildKit state volume
runner-docker-builder setup-remote --host ssh://ci@linux-builder.internal
```

For plain `docker build` on Colima's default builder (the VM daemon's embedded
BuildKit), manage `/etc/buildkit/buildkitd.toml` inside the VM:

```sh
runner-docker-builder gc configure --colima-vm --max-used-space 40
colima stop && colima start   # between jobs, to load the new policy
```

The VM write uses the Colima VM's own non-interactive sudo via `colima ssh`;
the script never invokes sudo on the macOS host.

## Registry mirror (pull-through cache)

A local `registry:2` pull-through cache removes repeated upstream downloads of
base images across jobs and runners:

```sh
runner-docker-builder registry-mirror setup
runner-docker-builder registry-mirror status
runner-docker-builder registry-mirror teardown   # keeps cached blobs; --remove-data deletes them
```

`setup` runs the `runner-registry-mirror` container with `--restart=always` and
a persistent volume, bound to `127.0.0.1:5001` by default (port 5000 is macOS
AirPlay Receiver). The target is the running Colima VM, or `--context CTX` /
`--host ssh://...` for another reachable Docker host. `--bind` accepts only
loopback or private/local-network addresses — the cache has no authentication
of its own and is never exposed publicly. Rerunning setup reuses a running
mirror container and its cached blobs.

The mirror is wired in two places:

- **BuildKit**: a `[registry."docker.io"]` stanza is merged into the managed
  per-builder `buildkitd.toml` (shared with `gc configure`), and builders are
  created with `--driver-opt network=host` so the builder container can reach
  the loopback mirror. As with GC policy, this applies at builder creation;
  recreate an existing builder with `docker buildx rm --keep-state` plus setup
  during a maintenance window — the helper will not remove it for you.
- **Docker daemon**: the mirror URL is merged into `registry-mirrors` in the
  daemon config — `${COLIMA_HOME:-~/.colima}/docker/daemon.json` for Colima,
  `${DOCKER_CONFIG:-~/.docker}/daemon.json` for Docker Desktop — preserving
  other keys. Remote daemons get printed instructions instead. Restart the
  daemon (or Colima) between jobs to load it.

Workflows need no changes: builds through the persistent builder and plain
`docker pull`/`docker build` use the mirror transparently once the builder and
daemon have been restarted as noted above. The mirror proxies
`https://registry-1.docker.io` by default (`--upstream` to change it) and never
accepts pushes.

## Existing builder migration and maintenance

The helper refuses to replace a builder if its driver/endpoint differs from
the expected configuration. Inspect it first with `docker buildx inspect
runner-remote`. For the old incorrect `remote` driver, remove only the obsolete
registration during a maintenance window, then rerun setup. For a
`docker-container` builder that must be recreated on the **same host**,
`docker buildx rm --keep-state NAME` preserves its state volume; reuse the
same builder/node name. It does not transfer volumes to a new host. See
[Docker's cache persistence instructions](https://docs.docker.com/build/builders/drivers/docker-container/).

Track `runner-docker-builder gc status --builder runner-remote` (or
`docker buildx du --builder runner-remote` directly), available disk, BuildKit
version, and job queue time. Preserve the cache during planned upgrades,
verify a representative build afterward, and schedule maintenance while jobs
are drained. The helper does not automatically upgrade an existing BuildKit
container, prune caches, or start Colima at boot.

## Measure before and after

Use a representative Dockerfile, fixed source/base image digests, the same
target platform, and the same output destination for each builder. Record
CPU/memory limits, concurrency, BuildKit version, network transfer, cache
import/export, and total job time.

```sh
# Substitute desktop-linux, runner-colima, and runner-remote in separate runs.
# --output avoids differences in image loading; replace with the same --push
# destination in each run when measuring end-to-end registry publication.
time docker buildx build --builder runner-remote --no-cache \
  --progress plain --output type=cacheonly .
time docker buildx build --builder runner-remote \
  --progress plain --output type=cacheonly .
# Make a small source-only change, then repeat the warm command.
```

`--no-cache` bypasses instruction-layer reuse; it does not erase downloaded
base images or persistent cache mounts. Use disposable builders for a truly
empty-cache comparison, without pruning production builders. Repeat each
case several times, compare medians, and separately measure the first build
after restoring registry cache on a fresh worker.

| Builder | Layers bypassed (s) | Warm, unchanged (s) | Warm, source change (s) | Full job (s) |
| --- | --- | --- | --- | --- |
| Docker Desktop | | | | |
| runner-remote | | | | |
| runner-colima | | | | |
