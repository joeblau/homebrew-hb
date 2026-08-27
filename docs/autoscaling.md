# Queue-aware autoscaling (`runner-autoscale`)

`runner-autoscale` is a controller that grows and shrinks the **local** runner
fleet based on the GitHub Actions job queue. It never provisions runners
itself — it delegates to `runner-setup` and `runner-cleanup`, so all of their
guarantees (LaunchDaemons running as the invoking user, no
`RUNNER_ALLOW_RUNASROOT`, sudo only for `/opt` and `/Library/LaunchDaemons`)
carry over unchanged.

## How it works

Each **tick** (one pass of the control loop):

1. **Observe** — `GET {scope}/actions/runs?status=queued` with `GITHUB_PAT`
   gives the queue depth (`total_count`). When the queue is empty, the
   controller also fetches `{scope}/actions/runners` to check that no local
   runner is `busy` before considering a scale-down.
2. **Decide** — bounded by `--min` / `--max`:
   - **Scale up** when queue depth `> --scale-up-threshold` (default 1) and
     current runners `< --max`: mint a fresh **registration token**
     (`POST {scope}/actions/runners/registration-token`) and run
     `RUNNER_TOKEN=... runner-setup SCOPE --runners current+1`.
     `runner-setup` skips already-configured runners, so exactly one new
     `runner-N` is configured. Registration tokens are scope-bound and
     short-lived, so a new one is fetched for **every** provision.
   - **Scale down** when the queue is empty, no local runner is busy, the
     fleet has been idle for at least `--cooldown-minutes` (default 30), and
     current runners `> --min`: mint a **removal token**
     (`POST {scope}/actions/runners/remove-token`) and run
     `RUNNER_REMOVE_TOKEN=... runner-cleanup --runner HIGHEST --yes`, which
     removes the highest-numbered runner. The cooldown timer resets after
     every removal, so a fleet drains one runner per idle cooldown period.
3. **Log** — every tick appends one line to the decision log
   (`/opt/github-runners/.autoscale/autoscale.log`):

   ```
   2026-08-27T20:30:00Z queue=3 runners=2 min=1 max=4 action=scale-up reason="queue depth 3 > threshold 1 and runners < max; provisioning runner-3 via runner-setup"
   ```

   Actions are `scale-up`, `scale-down`, `none`, `skip` (lock held), and
   `error`.
4. **Serialize** — a lockfile (`/opt/github-runners/.autoscale/lock`, an
   atomic `mkdir`) guarantees only one scale operation is ever in flight,
   across ticks, daemon and manual runs, and multiple hosts sharing nothing
   but the filesystem convention. Locks older than one hour are treated as
   stale (holder died mid-operation) and broken with a warning.

State (`IDLE_SINCE`, the log, and the lock) lives in
`/opt/github-runners/.autoscale/`. `runner-setup` chowns `/opt/github-runners`
to the invoking user, so the controller itself needs **no sudo**.

## Prerequisites

- At least one runner already provisioned with `runner-setup` (it creates and
  chowns `/opt/github-runners`). Register at the same scope you will autoscale.
- `runner-setup` and `runner-cleanup` on `PATH`.
- A PAT in `GITHUB_PAT` (or `GITHUB_PAT_FILE` pointing to a file containing
  it). Required scopes: `repo` for `--repo`, `admin:org` for `--org`. The PAT
  is never placed on a command line — curl reads the `Authorization` header
  from a 0600 config file in a private tmpdir.

## Running it

Single tick (what launchd runs):

```sh
GITHUB_PAT=github_pat_... runner-autoscale --org acme
```

Foreground daemon:

```sh
GITHUB_PAT=github_pat_... runner-autoscale --repo acme/monorepo \
  --daemon --interval 120 --min 1 --max 6
```

## Installing as a launchd service (StartInterval)

The recommended deployment is a system LaunchDaemon that runs one tick every
`StartInterval` seconds, as the **same non-root user** that owns the runners
(never root — `runner-setup` would refuse, and the runner processes must not
run as root).

1. Store the PAT in a root/admin-readable file instead of the plist
   (plists in `/Library/LaunchDaemons` are world-readable):

   ```sh
   sudo install -m 600 -o "$USER" /dev/null /etc/github-runner-autoscale-pat
   # paste the token into /etc/github-runner-autoscale-pat, then:
   sudo chmod 400 /etc/github-runner-autoscale-pat && sudo chown "$USER" /etc/github-runner-autoscale-pat
   ```

2. `runner-setup` / `runner-cleanup` call `sudo` internally for `/opt` and
   `/Library/LaunchDaemons`, and launchd provides no TTY for a password
   prompt. Grant scoped passwordless sudo for exactly the privileged
   subcommands they use, e.g. `/etc/sudoers.d/github-runner-autoscale`
   (`visudo`-checked, user `joe` shown — substitute your own):

   ```
   joe ALL=(root) NOPASSWD: /bin/mkdir, /usr/sbin/chown, /bin/rm, /bin/cp, /bin/chmod, /bin/launchctl, /usr/bin/xattr, /bin/ln
   ```

   This is deliberately broad-but-read-only-in-effect tooling; scope it
   further (argument lists) if your threat model requires it. Alternatively,
   run `runner-autoscale --daemon` from a logged-in session where `sudo`
   credentials are cached interactively.

3. `/Library/LaunchDaemons/com.github.runner-autoscale.plist`:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key>
     <string>com.github.runner-autoscale</string>
     <key>UserName</key>
     <string>joe</string>
     <key>ProgramArguments</key>
     <array>
       <string>/usr/local/bin/runner-autoscale</string>
       <string>--org</string>
       <string>acme</string>
       <string>--max</string>
       <string>6</string>
     </array>
     <key>EnvironmentVariables</key>
     <dict>
       <key>GITHUB_PAT_FILE</key>
       <string>/etc/github-runner-autoscale-pat</string>
       <key>PATH</key>
       <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
     </dict>
     <key>StartInterval</key>
     <integer>60</integer>
     <key>RunAtLoad</key>
     <true/>
     <key>StandardOutPath</key>
     <string>/opt/github-runners/.autoscale/launchd-stdout.log</string>
     <key>StandardErrorPath</key>
     <string>/opt/github-runners/.autoscale/launchd-stderr.log</string>
   </dict>
   </plist>
   ```

   ```sh
   sudo chown root:wheel /Library/LaunchDaemons/com.github.runner-autoscale.plist
   sudo chmod 644 /Library/LaunchDaemons/com.github.runner-autoscale.plist
   sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-autoscale.plist
   ```

`--daemon --interval 60` is an equivalent alternative when you prefer a
long-running process (e.g. under `tmux` or your own supervisor); use
`StartInterval` for the launchd-native deployment.

## Multi-host coordination: primary/standby

There is no distributed lock between Macs — two controllers polling the same
queue would both decide to scale and over-provision. Keep it simple:

- **One primary controller per scope.** Exactly one host runs
  `runner-autoscale` for a given org/repo, and it scales **its own** fleet
  (scale-up/down always operate on the local machine).
- **Other hosts run fixed fleets.** Provision them once with
  `runner-setup --runners N` and leave them out of autoscaling. Runner names
  are machine-prefixed (`HOST-runner-N`), so fleets never collide in GitHub.
- **Standby controller.** Keep the same plist installed but disabled on a
  second Mac:

  ```sh
  sudo launchctl disable system/com.github.runner-autoscale   # on the standby
  ```

  On failover, disable it on the old primary and
  `sudo launchctl enable system/com.github.runner-autoscale` + `kickstart` on
  the standby. The standby then scales **its** local fleet; the min/max bounds
  and decision log give you a clean audit trail of the handover.

If you genuinely need two elastic hosts in one scope, give each a different
`--max` and accept that both react to the same queue — the per-host lockfile
still serializes each host's operations, but total capacity can briefly
overshoot.

## API rate-limit math

A classic PAT gets **5,000 authenticated requests/hour** (15,000 for
GitHub Enterprise Cloud with a GitHub App, which this script does not use).

Requests per tick:

| Operation                              | Requests |
| -------------------------------------- | -------- |
| Queue depth (`actions/runs?status=queued`) | 1        |
| Runner busy check (only when queue is empty) | 0–1  |
| Scale-up/down (token mint, only when acting) | 1     |

Worst case ≈ 2 requests/tick, so:

| Interval | Requests/hour | % of 5,000 budget |
| -------- | ------------- | ----------------- |
| 30 s     | 240           | 4.8%              |
| 60 s (default) | 120     | 2.4%              |
| 300 s    | 24            | 0.5%              |

Formula: `requests/hour = (3600 / interval) × 2`, plus at most one token mint
per actual scale event. Even at a 30 s interval a single controller uses under
5% of the budget; don't run more than a handful of controllers against one PAT
at sub-minute intervals.

## Limitations

- **Label matching is approximate.** The `actions/runs?status=queued` endpoint
  reports queued *workflow runs*, not the labels their jobs request. The
  controller treats any queued run in the scope as demand and passes
  `--labels` through to `runner-setup` so new runners match your jobs. If a
  single scope serves heterogeneous labels, prefer separate scopes (or accept
  occasional over-provisioning). The `hosted-runners/queue` endpoints only
  cover GitHub-hosted larger runners and do not apply to self-hosted fleets.
- **Scale-up latency.** `runner-setup` re-resolves and re-downloads the runner
  tarball on each invocation, so a scale-up takes minutes, not seconds. The
  lockfile deliberately holds during this so nothing else scales meanwhile.
- **One runner per tick.** Both directions move a single runner per decision;
  bursty queues take `depth/threshold` ticks to fully absorb. This is
  intentional: it keeps every decision small, logged, and reversible.

## Troubleshooting

- Decision log: `/opt/github-runners/.autoscale/autoscale.log`
- Per-runner diagnostics: `/opt/github-runners/runner-N/_diag/`
- `action=error reason="scale-up aborted: could not mint a registration token"`
  → PAT lacks `repo`/`admin:org`, or expired.
- Ticks failing under launchd but fine interactively → sudoers entries missing
  (step 2 above); check the launchd stderr log.
- A wedged `lock` directory after a hard kill is broken automatically after
  one hour, or remove `/opt/github-runners/.autoscale/lock` manually.
