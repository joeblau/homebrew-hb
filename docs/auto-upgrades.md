# Automatic runner version upgrades

`runner-upgrade` keeps the actions/runner binaries that `runner-setup`
installed under `/opt/github-runners` up to date, without re-registering
anything: the registration state (`.runner`, `.credentials`) and the `_work`
directory are carried over, so runner identity and labels survive every
upgrade.

## Commands

```sh
runner-upgrade check --all          # report which runners are behind (exit 3 = upgrades pending)
runner-upgrade run --all --yes      # canary-first upgrade
runner-upgrade rollback --runner 2  # restore runner-2.prev over runner-2
```

`run` is **canary-first**:

1. The first selected runner (runner-1) is quiesced — its system daemon
   `com.github.runner-1` is booted out.
2. The new tarball is downloaded and SHA-256 verified exactly like
   `runner-setup` does (same releases API, same `<!-- BEGIN SHA osx-* -->`
   marker parsing, same `shasum` verification).
3. The old install is moved aside to `runner-1.prev`, the new binaries are
   extracted, and `.runner` / `.credentials*` / `.path` / `.env` / `_work`
   are carried over from the previous install.
4. The existing LaunchDaemon is re-bootstrapped and health is verified by
   waiting (up to 120 s) for the newest `_diag/Runner_*.log` to print
   `Listening for Jobs`.
5. If **any** of that fails, `runner-1.prev` is restored automatically and
   the remaining runners are left untouched.
6. Only after a healthy canary are the remaining runners rolled the same way.

Every event (checks, upgrades, rollbacks, failures) is appended to the stable
log `/opt/github-runners/upgrade.log`.

## Scheduling with launchd

Run `check`/`run` on a timer with a system LaunchDaemon. Two constraints come
straight from the runner's own rules:

- the job must **not run as root** — the script refuses to manage runners as
  root, so the plist needs a `UserName` key naming the runner owner (the same
  user `runner-setup` ran as);
- that user needs passwordless sudo for the few privileged operations
  (`launchctl bootstrap/bootout`, file ops under `/opt` and
  `/Library/LaunchDaemons`). Grant it narrowly.

### sudoers

Create `/etc/sudoers.d/github-runner-upgrade` (with `visudo`), replacing
`joe` with the runner owner:

```
joe ALL=(root) NOPASSWD: /bin/mkdir, /bin/mv, /bin/rm, /bin/cp, /usr/sbin/chown, /usr/bin/touch, /bin/launchctl
```

### Sample plist

Save as `/Library/LaunchDaemons/com.github.runner-upgrade.plist`, owned
`root:wheel`, mode `644`. This checks and upgrades every 6 hours
(`StartInterval 21600`); use `StartCalendarInterval` instead if you prefer a
fixed daily time. Replace `joe` and the `runner-upgrade` path.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.github.runner-upgrade</string>
  <key>UserName</key>
  <string>joe</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/runner-upgrade</string>
    <string>run</string>
    <string>--all</string>
    <string>--yes</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/Users/joe</string>
  </dict>
  <key>StartInterval</key>
  <integer>21600</integer>
  <key>StandardOutPath</key>
  <string>/opt/github-runners/upgrade-launchd-stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/opt/github-runners/upgrade-launchd-stderr.log</string>
</dict>
</plist>
```

Load it:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-upgrade.plist
```

`--yes` is required in the plist: without a TTY the script refuses to upgrade
unless confirmation is bypassed.

## Rollback

After a successful `run`, each runner's previous install remains as
`runner-N.prev`. To revert a runner:

```sh
runner-upgrade rollback --runner 2      # one runner
runner-upgrade rollback --all           # every runner that has a .prev
```

Rollback boots out the daemon, swaps `runner-N.prev` back into place,
re-bootstraps, and health-checks the same way. Once you are satisfied with an
upgrade, delete the `.prev` directories to reclaim disk.
