# Runner health checks and alerting

`runner-health` is a watchdog for the self-hosted GitHub Actions runners that
`runner-setup` provisions. It detects dead, stuck, and unregistered runners,
restarts them via launchd, and raises an alert when something needs human
attention.

## What it checks

Every pass, for each `runner-N` under `/opt/github-runners` (plus any orphaned
`com.github.runner-N` daemon whose directory is gone):

1. **launchd state** — `launchctl print system/com.github.runner-N` must
   succeed; the service `state` is expected to be `running`.
2. **Diag freshness** — while the service claims to be running, the newest
   `/opt/github-runners/runner-N/_diag/*.log` must be younger than
   `--stale-minutes` (default **15**), and the newest `Runner_*.log` must
   contain `Listening for Jobs` (the runner's idle-loop marker). Runners
   executing a job stay fresh through their `Worker_*.log` files and are not
   flagged.
3. **Console session (optional, `--require-gui`)** — the runner owner (the
   `UserName` of the runner daemons) must have a launchd `gui/<uid>` session.
   WebKit, Chromium and the iOS simulators hang without one (WebKit fails on
   `NSXPCSharedListener 'ClientCallsAuxiliary'`, then its launch timeout), and
   a Mac sitting at the login window has none. Reported as the pseudo-runner
   `host` with `svc_state` `session` or `no-session`. Only a login creates the
   session; automatic login does so at boot. With `--reboot-for-gui` (which
   implies `--require-gui`) the watchdog restarts the host when the session is
   missing **and** automatic login is set for the runner owner, no
   `Runner.Worker` is running, no runner is in maintenance, and nobody else
   owns `/dev/console`. A reboot blocked by a running job or maintenance is
   reported as `result="deferred"` and retried next pass; one that automatic
   login could not fix is `failed` with the reason (run `runner-bootstrap`
   interactively to turn automatic login on). One missing session causes at
   most one reboot: the watchdog records the boot session it rebooted from in
   `/opt/github-runners/.health/host.rebooted_in`, and if the Mac comes back
   still without a session, automatic login is not working and it alerts
   instead of looping. The marker clears as soon as a session is seen.
4. **GitHub API (optional)** — with `--org ORG` or `--repo OWNER/REPO` and a
   `GITHUB_PAT`, it queries
   `GET /orgs/ORG/actions/runners` (or `.../repos/OWNER/REPO/...`), parses the
   `status`/`busy` fields without `jq`, and flags runners that are `offline`,
   missing from the list (unregistered), or `busy` for longer than
   `--busy-timeout-minutes` (default **120**). The API does not report how long
   a runner has been busy, so the first-seen-busy timestamp is persisted in
   `/opt/github-runners/.health/` across passes.

## Remediation

| Condition                                   | Action                                   |
| ------------------------------------------- | ---------------------------------------- |
| Stuck busy / stale diag while `running`     | `launchctl kickstart -k system/<label>`  |
| Offline / unregistered / loaded but not running | bootout + bootstrap of the plist     |
| Daemon not loaded, plist present            | bootstrap (and enable + kickstart)       |
| Daemon **and** plist both missing           | No local fix — alert: re-run `runner-setup` |
| Runner directory gone, daemon/plist remains | No local fix — alert: run `runner-cleanup` or re-run `runner-setup` |
| Console session missing (`--require-gui`)   | `shutdown -r now` with `--reboot-for-gui` when idle and automatic login is on; otherwise alert |

A pass exits `0` when every runner is healthy or was successfully remediated,
and `1` when any issue could not be fixed.

## Alerting

- **Structured log (always):** one key=value line per runner per pass on
  stdout, e.g.

  ```
  ts="2026-08-27T20:30:00Z" host="macmini" prog="runner-health" runner="runner-1" event="diag_stale" svc_state="running" diag_age_min=42 api_status="online" busy=false action="kickstart" result="remediated"
  ```

- **Webhook (optional):** when `ALERT_WEBHOOK_URL` is set, a Slack-compatible
  `{"text": "..."}` JSON payload is POSTed **once per incident** — a marker in
  `/opt/github-runners/.health/` suppresses repeats until the runner recovers.
  Works with Slack incoming webhooks and any service that accepts the same
  payload shape (e.g. Discord's Slack-compatible endpoint).

Both secrets (`GITHUB_PAT`, `ALERT_WEBHOOK_URL`) are read from the environment
only and passed to `curl` through a 0600 config file in a private temp dir, so
they never appear in `ps` output or logs.

## Deployment: launchd StartInterval timer

`runner-setup` installs this for you: after every successful run it writes
`/Library/LaunchDaemons/com.github.runner-health.plist` (root, mode 644, no
secrets) running `runner-health --once --reboot-for-gui` every 300 seconds,
with logs under `/Library/Logs/runner-health/`, and loads it into the system
domain. An existing plist is left exactly as it is, so edit it to add
`--org`/`GITHUB_PAT`/`ALERT_WEBHOOK_URL` (then `chmod 600` it, see below) or
to remove `--reboot-for-gui` on a fleet that runs no GUI jobs; `runner-setup
--skip-health` installs nothing.

`runner-health --once` is designed to be launched periodically by launchd. Run
it as a **root LaunchDaemon** — checking the system domain requires root, and
the watchdog itself never runs runner processes (those still run as the
provisioning user via their own daemons). To write the plist by hand, save
this as `/Library/LaunchDaemons/com.github.runner-health.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.github.runner-health</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/runner-health</string>
    <string>--once</string>
    <string>--reboot-for-gui</string>
    <string>--org</string>
    <string>YOUR_ORG</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GITHUB_PAT</key>
    <string>ghp_YOUR_TOKEN</string>
    <key>ALERT_WEBHOOK_URL</key>
    <string>https://hooks.slack.com/services/YOUR/WEBHOOK/PATH</string>
  </dict>
  <key>StartInterval</key>
  <integer>300</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/runner-health.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/runner-health.log</string>
</dict>
</plist>
```

Notes:

- Adjust the `runner-health` path for your Homebrew prefix
  (`/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin` on Intel).
- `--org`/`--repo` and the `EnvironmentVariables` block are optional; without
  them only the local launchd + diag checks run.
- The `GITHUB_PAT` needs read access to self-hosted runners (org/repo
  **Administration** permission for fine-grained PATs, or `admin:org` /
  `repo` for classic tokens).
- **The plist contains secrets.** LaunchDaemon plists are world-readable by
  default, so restrict it:

  ```sh
  sudo chown root:wheel /Library/LaunchDaemons/com.github.runner-health.plist
  sudo chmod 600 /Library/LaunchDaemons/com.github.runner-health.plist
  ```

Load it with:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-health.plist
```

Every 300 seconds launchd runs one pass; pass/fail lines and alerts land in
`/var/log/runner-health.log`.

## Alternatives

- **cron** (any POSIX cron, runs as your user — it will prompt for sudo once,
  then the cached timestamp applies):

  ```
  */5 * * * * GITHUB_PAT=ghp_... ALERT_WEBHOOK_URL=https://hooks.slack.com/services/... /opt/homebrew/bin/runner-health --once --org YOUR_ORG >> /var/log/runner-health.log 2>&1
  ```

- **Watch mode** — `runner-health --interval 300` loops forever in the
  foreground; handy for a terminal session while debugging. Prefer the
  LaunchDaemon for anything permanent.

## Exit status

- `0` — all runners healthy, or every detected issue was remediated (a reboot
  that was issued counts as remediated).
- `1` — at least one issue could not be fixed locally (e.g. daemon and plist
  both missing → re-run `runner-setup`), a remediation failed, or a reboot
  for a missing console session was deferred.
- `2` — usage error.

## Diagnosis without remediation

`runner-health --once --dry-run` runs every check but never restarts a
runner, sends an alert, or clears alert markers; issues are reported with
`result="detected"` and the exit status is `1` when anything was found. Add
`--json` to get one document per pass instead of log lines and the summary
table. Both are what `runner-mcp` exposes to coding agents; see
[agent-tools.md](agent-tools.md) for the schema and a sudoers rule that keeps
the launchd read non-interactive.
