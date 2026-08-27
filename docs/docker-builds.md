# Fast Docker image builds on macOS runners

Docker Desktop on macOS builds every image inside a Linux VM. Each build pays
for the VM's CPU throttling, memory cap, and (for bind-mounted contexts) the
`osxfs`/`virtiofs` file-sharing layer — on multi-stage builds this routinely
costs 3–10× versus native Linux. Commercial runner vendors (e.g. Blacksmith)
advertise ~40× faster Docker builds largely by moving builds onto bare-metal
Linux builders.

`runner-docker-builder` (at the repo root) wires the docker buildx CLI on your
macOS self-hosted runners to a faster builder, without changing any workflow
YAML: once a builder is the default for the runner user, plain `docker build`
and `docker/build-push-action` use it automatically.

## Options

### Option A — remote Linux builder (recommended)

Build on any reachable Linux machine over SSH via the buildx `remote` driver.
Builds run on native Linux — no VM layer at all. Any always-on Linux box (an
old PC, a cloud VM, a spare mini) works; it only needs Docker and SSH access
from the runner user.

Prerequisites:

- A Linux host with Docker installed; the remote user can run `docker`
  (member of the `docker` group).
- SSH key auth from the runner's macOS user to the remote user
  (`ssh user@linux-builder` must succeed non-interactively).

Setup (run as the same non-root user the runner daemons run as):

```sh
runner-docker-builder setup-remote --host ssh://ci@linux-builder.internal

# or via env var, and pin target platforms:
BUILDER_SSH_HOST=ssh://ci@10.0.0.5 runner-docker-builder setup-remote \
    --platform linux/amd64,linux/arm64
```

This creates (or verifies) a buildx builder named `runner-remote` with the
`remote` driver, installs the buildx CLI plugin via Homebrew if missing, and
sets `runner-remote` as the default builder for the user.

### Option B — local colima VM (no remote machine available)

[colima](https://github.com/abiosoft/colima) runs a lightweight Lima VM —
less overhead than Docker Desktop, and free of its licensing terms. Slower
than native remote Linux, but fully local:

```sh
runner-docker-builder setup-colima                  # defaults: 4 cpu, 8 GiB, 60 GiB
runner-docker-builder setup-colima --cpu 6 --mem 12 --disk 100
```

This installs `colima`, the `docker` CLI, and `docker-buildx` via Homebrew,
starts the VM, and creates a `docker-container`-driver buildx builder named
`runner-colima` set as default. The `docker-container` driver (unlike the
plain `docker` driver) supports full cache export and multi-platform builds.

### Revert / inspect

```sh
runner-docker-builder use-desktop   # back to Docker Desktop's desktop-linux builder
runner-docker-builder status        # show active context, builders, and mode
```

## Layer caching

A fast builder only stays fast if layers are reused. Wire a shared cache into
`docker/build-push-action` so layers survive across runner hosts and reboots.

### Registry cache (simplest — works today)

Push cache metadata to a tag in the same registry you already push to:

```yaml
cache-from: type=registry,ref=ghcr.io/OWNER/REPO:buildcache
cache-to:   type=registry,ref=ghcr.io/OWNER/REPO:buildcache,mode=max
```

`mode=max` exports cache for every stage of a multi-stage build (default
`mode=min` only exports the final image's layers). Registry auth reuses the
`docker/login-action` credentials already in the workflow.

### S3 cache backend (optional companion)

If you deploy the shared cache backend from the companion issue — see
**docs/cache-backend.md** — point buildx at it with the `s3` cache driver.
Keep credentials in environment variables / CI secrets, never in the YAML:

```sh
# runner environment (e.g. exported in the LaunchDaemon env or job env)
export ACTIONS_CACHE_S3_BUCKET="actions-cache"
export ACTIONS_CACHE_S3_ENDPOINT="https://cache.internal:9000"   # any S3 API
export ACTIONS_CACHE_S3_REGION="us-east-1"
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY come from CI secrets
```

```yaml
cache-from: type=s3,bucket=${{ env.ACTIONS_CACHE_S3_BUCKET }},region=${{ env.ACTIONS_CACHE_S3_REGION }},endpoint_url=${{ env.ACTIONS_CACHE_S3_ENDPOINT }},name=app-image
cache-to:   type=s3,bucket=${{ env.ACTIONS_CACHE_S3_BUCKET }},region=${{ env.ACTIONS_CACHE_S3_REGION }},endpoint_url=${{ env.ACTIONS_CACHE_S3_ENDPOINT }},name=app-image,mode=max
```

The env hook names above are deliberately generic — any S3-compatible service
works; see docs/cache-backend.md for the concrete backend deployment.

### Inline cache (for plain `docker build` consumers)

If some consumers only `docker pull` and build with the classic builder, also
embed inline metadata so the pulled image itself acts as a cache source:

```yaml
cache-from: type=registry,ref=ghcr.io/OWNER/REPO:latest
build-args: BUILDKIT_INLINE_CACHE=1
```

`cache-inline` (via `cache-to: type=inline`) writes cache metadata into the
image config; it only caches the final stage, so prefer `mode=max` registry
or S3 cache for multi-stage builds.

## Drop-in workflow

Works unchanged on the macOS self-hosted runners after
`runner-docker-builder setup-remote ...` — the named builder is already the
user's default, so `docker/setup-buildx-action` picks it up via `driver-opts`
or you can pin it explicitly:

```yaml
name: build
on: [push]

jobs:
  docker:
    runs-on: [self-hosted, macos]
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3
        with:
          # builder name created by runner-docker-builder; omit to use the
          # runner user's current default (already runner-remote).
          buildx-version: latest

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ghcr.io/${{ github.repository }}:latest
          cache-from: type=registry,ref=ghcr.io/${{ github.repository }}:buildcache
          cache-to: type=registry,ref=ghcr.io/${{ github.repository }}:buildcache,mode=max
```

For a one-off explicit remote endpoint (no pre-provisioned builder):

```yaml
      - uses: docker/setup-buildx-action@v3
        with:
          driver: remote
          endpoint: ssh://ci@linux-builder.internal
```

## Measuring the baseline (required before/after comparison)

Run this repeatable benchmark on the same Mac, once with Docker Desktop as
the default builder and once against the remote builder. Use a representative
multi-stage Dockerfile; this sample exercises dependency caching and a
compile stage:

```dockerfile
# Dockerfile.bench
FROM golang:1.22 AS deps
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

FROM deps AS build
COPY . .
RUN go build -o /bin/app ./...

FROM gcr.io/distroless/static
COPY --from=build /bin/app /bin/app
ENTRYPOINT ["/bin/app"]
```

Methodology — identical machine, identical source tree, warm daemon:

```sh
# 1. Record the active builder
runner-docker-builder status

# 2. Cold build (no cache at all — measures raw build throughput)
docker buildx build --no-cache --pull -f Dockerfile.bench -t bench:run .

# 3. Warm build, no source changes (measures local layer-cache hit)
time docker buildx build -f Dockerfile.bench -t bench:run .

# 4. Warm build with a one-line source change (measures incremental rebuild)
time docker buildx build -f Dockerfile.bench -t bench:run .

# 5. Repeat 2–4 after switching builders:
runner-docker-builder use-desktop      # baseline: Docker Desktop
runner-docker-builder setup-remote --host ssh://ci@linux-builder.internal
```

Record results in this table (add it to your PR or ops notes):

| Builder        | Cold build (s) | Warm, no change (s) | Warm, 1-line change (s) | Speedup vs Desktop |
|----------------|----------------|---------------------|--------------------------|--------------------|
| Docker Desktop |                |                     |                          | 1.0×               |
| runner-remote  |                |                     |                          |                    |
| runner-colima  |                |                     |                          |                    |

Expect the cold-build column to show the largest gap (native Linux CPU/IO vs
the Desktop VM) and the warm columns to converge toward the cache backend's
latency — which is why the layer caching section matters on the remote path:
without a shared cache, a fresh remote builder starts cold.

## Notes

- Everything runs as the invoking non-root user; `runner-docker-builder`
  never uses sudo and stores all state in `~/.docker` and `~/.colima`.
- The remote builder's layers live on the Linux host — size its disk for the
  cache you intend to keep, and prune on a schedule (`docker buildx prune`).
- Multi-arch (`--platform linux/amd64,linux/arm64`) offloads emulation to
  QEMU on the Linux builder; install `qemu-user-static`/`binfmt` there if you
  need foreign-arch stages.
