# Automated disk cleanup between jobs

Self-hosted runners accumulate disk usage fast: every job leaves a `_work`
directory (checkout, build output, per-job tool caches), and user-level tool
caches (npm, yarn, pip, gradle, maven, Homebrew) grow without bound. Left
alone, a runner eventually fills its disk and every job on it starts failing
with obscure "no space left" errors.

`runner-prune` (installed by this tap) is a post-job cleanup script that:

1. purges each `/opt/github-runners/runner-N/_work` directory,
2. clears common tool caches in the runner user's home — each guarded, so a
   missing directory is fine,
3. optionally prunes Docker (`docker system prune -af --volumes`) — only when
   docker is installed **and** `RUNNER_PRUNE_DOCKER=1`,
4. enforces a minimum free-disk threshold: when free space is still below
   `--min-free-gb` (default 20) **after** cleanup, it prints a clear refusal
   and exits with code **75**, so an operator or scheduler can block new jobs.

It runs as the invoking non-root user, never sets `RUNNER_ALLOW_RUNASROOT`,
and uses `sudo` only as a non-interactive fallback for root-owned files a job
left under `/opt/github-runners`.

## Usage

```sh
runner-prune                          # post-job cleanup, 20 GiB floor
runner-prune --min-free-gb 50         # stricter floor
runner-prune --runners-dir /data/runners
runner-prune --dry-run                # report sizes, delete nothing
RUNNER_PRUNE_DOCKER=1 runner-prune    # also reclaim Docker images/volumes
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Cleanup done; free space is at or above the threshold. |
| 1    | An operation failed (see the log). |
| 2    | Usage error (bad arguments). |
| 75   | Free disk is below `--min-free-gb` after cleanup — refuse new jobs. |

Every action is logged to stderr with a timestamp, so output captured by the
runner (hook logs) or launchd can be correlated with job times.

## Wiring it as a post-job hook (recommended)

The actions runner (>= **2.296**) supports job hooks: a script pointed to by
`ACTIONS_RUNNER_HOOK_JOB_COMPLETED` runs automatically after **every** job,
and its stdout/stderr are captured in the job log and under the runner's
`_diag` directory.

Create a wrapper (adjust the `runner-prune` path to your brew prefix —
`/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin` on Intel):

```sh
sudo mkdir -p /opt/github-runners/hooks
sudo tee /opt/github-runners/hooks/job-completed.sh >/dev/null <<'EOF'
#!/bin/bash
exec /opt/homebrew/bin/runner-prune --min-free-gb 20
EOF
sudo chmod +x /opt/github-runners/hooks/job-completed.sh
```

Then set the environment variable for each runner's LaunchDaemon. Edit
`/Library/LaunchDaemons/com.github.runner-N.plist` (one per runner) and add to
the existing `EnvironmentVariables` dict:

```xml
<key>ACTIONS_RUNNER_HOOK_JOB_COMPLETED</key>
<string>/opt/github-runners/hooks/job-completed.sh</string>
```

Reload each daemon so the change takes effect:

```sh
sudo launchctl bootout system/com.github.runner-1
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-1.plist
```

From then on, cleanup runs after every job on every runner, and the
timestamped `runner-prune` output shows up in the job's log and in
`/opt/github-runners/runner-N/_diag/`.

> If free space is still below the threshold after cleanup, the hook exits 75
> and the refusal is visible in the job log. The job itself is already done by
> then, so the hook cannot fail it — treat exit 75 as a signal to take the
> runner offline (GitHub UI: Runners > select runner > Disable) until an
> operator frees space.

## Fallback: periodic launchd job

If your runner version predates job hooks (< 2.296), or you want a belt-and-
suspenders sweep, run `runner-prune` on a timer. Install a user LaunchAgent —
no sudo, runs as the runner user:

`~/Library/LaunchAgents/com.github.runner-prune.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.github.runner-prune</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/runner-prune</string>
    <string>--min-free-gb</string>
    <string>20</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>RUNNER_PRUNE_DOCKER</key>
    <string>0</string>
  </dict>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/opt/github-runners/runner-prune.log</string>
  <key>StandardErrorPath</key>
  <string>/opt/github-runners/runner-prune.log</string>
</dict>
</plist>
```

Load it:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.runner-prune.plist
```

`StartInterval` (seconds) controls the cadence — hourly (3600) is a sensible
default for busy machines; daily (86400) is fine otherwise.

## Safety notes

- `--dry-run` reports every path and its size without deleting anything; run
  it once by hand before wiring up the hook.
- The script deletes build output and caches only. It never touches runner
  registration files (`.runner`, `.credentials`) or the LaunchDaemon plists.
- Docker pruning is destructive to **all** local images, containers, networks
  and volumes, so it is strictly opt-in via `RUNNER_PRUNE_DOCKER=1`.
- The low-disk refusal (exit 75) is checked **after** cleanup, so the value
  you get reflects the best the machine can do on its own.
