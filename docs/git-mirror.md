# Local Git mirrors (`runner-git-mirror`)

`runner-git-mirror` keeps bare mirrors of GitHub repositories on the runner
Mac and rewrites checkouts to read from them, so `actions/checkout` clones
over the local disk instead of the network. It closes the "safe Git mirror
service" gap noted in [the performance review](runner-performance.md). It is
a plain file-based mirror — not the Blacksmith checkout-caching service — and
it changes no workflow files.

Mirrors live under `/opt/github-runner-git/mirrors`, one bare repository per
mirrored repo (`<owner>--<repo>.git`). The service runs as the invoking
non-root user, mirroring `runner-cache`: sudo is used only for `/opt` and
`/Library/LaunchDaemons`.

## Trust boundary — read this first

A mirror is a **plain directory on the host**. Every job that runs as the
runner user on this Mac can read every mirrored repository's full history,
regardless of GitHub permissions. Only mirror repositories whose contents may
be visible to **every workload on this host**. For a fleet that mixes trust
levels, mirror only the repositories inside the host's trust boundary and let
everything else fall through to the network (that is the default for
unmirrored repos).

Mirror credentials are equally sensitive: a read-only PAT for a private
mirror is stored 0600 under `/opt/github-runner-git/config/tokens`, readable
by the runner user. Never mirror a repository — or reuse a token — across a
trust boundary the host does not already share.

## Add and refresh mirrors

```sh
runner-git-mirror add --repo acme/monorepo
runner-git-mirror add --repo acme/private --token-file ~/.github-mirror-pat
runner-git-mirror add --repo acme/enterprise --url https://git.acme.example/acme/enterprise.git
runner-git-mirror update                 # fetch --prune every mirror now
runner-git-mirror update --repo acme/monorepo
```

`add` performs the initial `git clone --mirror` (which records the
`+refs/*:refs/*` fetch refspec, so updates track branch/tag deletion via
`--prune`), writes a config record under `config/repos`, and installs the
`com.github.runner-git-mirror` LaunchDaemon on first use. The timer runs
`runner-git-mirror update` every 300 seconds (`StartInterval`), as the
invoking user, logging to `/opt/github-runner-git/logs`. `remove --repo`
deletes the mirror, its config/token/state, regenerates installed runner
gitconfigs, and removes the timer when the last mirror goes away.

Each update takes a per-mirror lock (`<mirror>.git.lock`, an atomic `mkdir`
with the holder's PID inside). A concurrent timer tick or job-triggered
`update` skips a mirror whose lock is held by a live process, and reclaims
the lock when the holder PID is dead. A failed fetch leaves the mirror at its
last good state and is counted in the mirror's state file.

### Private repositories and credential handling

`--token-file PATH` copies a read-only token into
`config/tokens/<slug>` (mode 0600) at add time. Fetches and the initial clone
reach the token only through the generated askpass wrapper
(`bin/git-mirror-askpass.sh`, wired via `GIT_ASKPASS` with
`GIT_TERMINAL_PROMPT=0`): the token never appears on a git command line where
`ps` could expose it, never in the world-readable LaunchDaemon plist, and
never in config files besides the 0600 token file. Rotate by re-running `add`
with a fresh `--token-file` after `remove`.

All service-side git operations run with `GIT_CONFIG_GLOBAL=/dev/null` and
`GIT_CONFIG_NOSYSTEM=1`, so the installed `insteadOf` rewrites (or any
operator/machine gitconfig) can never redirect the mirror's own fetch — e.g.
back onto the mirror itself, which would silently report success while going
stale.

## Route checkouts through the mirrors

```sh
runner-git-mirror install-git-config --all        # every runner under /opt/github-runners
runner-git-mirror install-git-config --runner 2   # one runner
runner-git-mirror uninstall-git-config --all      # revert
```

For each selected runner this generates
`/opt/github-runner-git/gitconfig/runner-N.gitconfig` and adds one line to
`/opt/github-runners/runner-N/.env`:

```
GIT_CONFIG_GLOBAL=/opt/github-runner-git/gitconfig/runner-N.gitconfig
```

`bin/runsvc.sh` sources `.env` before every job, so the setting applies to
all job steps without touching shared `~/.gitconfig`. If the invoking user's
`~/.gitconfig` exists at generation time, the generated file `[include]`s it,
so existing global settings (credential helpers, signing) still apply.
`install-git-config` is idempotent; `add`/`remove` regenerate already
installed gitconfigs automatically. Restart an idle runner
(`sudo launchctl kickstart -k system/com.github.runner-N`) for a running
service to pick up a new `.env`.

### What the generated gitconfig does

Per mirror it contains forward rules and one reverse repair rule:

```gitconfig
[url "/opt/github-runner-git/mirrors/acme--monorepo.git"]
	insteadOf = https://github.com/acme/monorepo
	insteadOf = https://github.com/acme/monorepo.git
	insteadOf = git@github.com:acme/monorepo
	insteadOf = git@github.com:acme/monorepo.git
	insteadOf = ssh://git@github.com/acme/monorepo
	insteadOf = ssh://git@github.com/acme/monorepo.git
[url "https://github.com/acme/monorepo.git"]
	insteadOf = /opt/github-runner-git/mirrors/acme--monorepo.git
```

- git applies `insteadOf` at **transport time only** (longest prefix match;
  both `.git` and non-`.git` spellings are listed). `actions/checkout` passes
  the canonical URL; the fetch is served from the local bare mirror, while
  `remote.origin.url` recorded in the checkout stays the canonical URL — so
  **no post-clone repair step is needed** and the checkout looks identical to
  a network clone.
- The reverse rule rewrites the mirror path back to the canonical URL, so
  nothing (including a stray job that references the mirror path directly)
  treats the mirror itself as a remote to push to or fetch from.
- Repositories with no mirror match no rule and **fall through to the
  network** unchanged. A stale mirror still works (it is a valid git repo);
  freshness is on the operator via `status`/`metrics`.

One caveat for manual testing: `insteadOf` rewrites URLs that go through a
git transport (https, ssh, `file://`). A plain local filesystem path is
stat'ed by `git clone` before rewriting, so path-based "URLs" do not get
rewritten — irrelevant for real GitHub remotes.

## Status and metrics

```sh
runner-git-mirror list             # repo, canonical URL, size
runner-git-mirror status           # per-mirror last fetch, age, status, sizes
runner-git-mirror status --json    # same facts as one JSON object
runner-git-mirror metrics          # totals, failures, oldest age, coverage
```

`status` reads each mirror's state file (`state/<slug>.state`: last fetch
epoch, status, duration, cumulative fetch/failure counts). `metrics`
aggregates those counters and reports **git-config coverage** — how many
provisioned runners have the `.env` wiring. Actual checkout hit rates are not
observable from host state; measure clone durations in jobs (e.g. compare the
"Checkout" step before and after `install-git-config`) rather than deriving a
percentage here.

## Operations notes

- Mirrors grow with upstream history; watch `/opt/github-runner-git` disk
  usage with `status`. There is no automatic expiry — `remove` mirrors you no
  longer need.
- Troubleshoot the timer with `sudo launchctl print system/com.github.runner-git-mirror`
  and `/opt/github-runner-git/logs/update-stderr.log`.
- If the timer plist was removed manually, re-run any `add` (after `remove`)
  to reinstall it.
- The tool ships in this repository but is not yet wired into
  `Formula/runner-setup.rb`; run it from a checkout or install it on PATH
  yourself.

| Command | Behavior |
| --- | --- |
| `add --repo OWNER/REPO [--url URL] [--token-file PATH]` | Clone the bare mirror, record config, install the refresh timer on first use. |
| `remove --repo OWNER/REPO` | Delete the mirror, config, token, and state; refresh installed gitconfigs; remove the timer when none remain. |
| `list` | Show mirrored repositories with canonical URL and size. |
| `update [--repo OWNER/REPO]` | `git fetch --prune` one or all mirrors under a per-mirror lock. |
| `install-git-config (--runner N \| --all)` | Generate per-runner gitconfig and wire `.env` via `GIT_CONFIG_GLOBAL`. |
| `uninstall-git-config (--runner N \| --all)` | Remove the `.env` line and generated gitconfig. |
| `status [--json]` | Timer state plus per-mirror freshness and sizes. |
| `metrics` | Aggregate counters and runner coverage; no fabricated hit rate. |
