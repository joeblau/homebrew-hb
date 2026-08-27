# Ephemeral self-hosted runners

Ephemeral runners are GitHub Actions self-hosted runners that pick up **exactly
one job** and are then automatically removed by GitHub. Each job starts on a
pristine machine image (or, here, a pristine runner directory), which is the
recommended hardening for running untrusted CI: no leftover build artifacts,
credentials, or processes can leak from one job into the next.

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
         3. wipe _work/ and .runner/.credentials*/.env/.path
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
- **Failure handling.** A failed token fetch or `config.sh` run retries with
  exponential backoff (10s → 20s → 40s … capped at 300s). After 5 consecutive
  failures the supervisor exits non-zero, and launchd's `KeepAlive` +
  `ThrottleInterval` restarts it — the daemon self-heals without crash-looping.
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

The PAT is used only in an `Authorization: Bearer` header and is never logged.
The short-lived registration token is passed to `config.sh` as an argument
(`config.sh` has no env alternative), so it is briefly visible in `ps` to other
local users — the same trade-off `runner-setup` already makes.

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
Enterprise), `--max-failures` — see `runner-ephemeral --help`.

## Notes and caveats

- Ephemeral re-registration adds a few seconds of latency before each job
  (token fetch + `config.sh`). Throughput-sensitive farms should size N
  accordingly.
- `_work` is wiped between cycles; job workspaces do not persist. `_diag`
  logs intentionally persist so failures remain diagnosable.
- A `--url` scope with a path deeper than `OWNER/REPO` cannot be mapped to an
  API endpoint — use `--org`/`--repo` (and `--api-url` for GHE) in that case.
