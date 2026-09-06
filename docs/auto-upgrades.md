# Runner shutdown, upgrade, and recovery

Tooling **v1.9.0** adds explicit maintenance controls to `runner-upgrade`.
Tooling **v1.9.1** adds explicit organization/repository selection during repair.
Homebrew's tooling version is separate from GitHub's runner binary version:

```sh
brew update
brew upgrade joeblau/hb/runner-setup
runner-upgrade check --all
```

`check` reports installed binary versions. A runner listed as `current` is not
necessarily connected to GitHub; `start`, `repair`, and `run` verify startup.

## Maintenance flow

Pause dispatch of new jobs and allow active jobs to finish first. The helper
refuses shutdown when it sees an active local `Runner.Worker` or cannot inspect
worker state. This is a best-effort check, not an atomic GitHub queue drain.

```sh
# Optional explicit shutdown; retains registration, workspace, and plists.
runner-upgrade shutdown --all --yes

# Verify the download, shut down the selection, upgrade and restart it.
runner-upgrade run --all --yes

# Resume stopped runners without changing their installed binary versions.
runner-upgrade start --all --yes
```

`run` performs shutdown and startup itself; separate `shutdown` is useful for a
longer maintenance window. If you shut down explicitly, a later download or
preflight failure leaves those runners stopped until you run `start`.

Every command accepts `--runner N` to select one runner. `check`, `shutdown`,
`run`, and `start` also accept `--runners N` for runner-1 through runner-N, or
`--all` for the fleet. Selectors cannot be combined. For example, when runner-2
is already upgraded, repair and upgrade only runner-1:

```sh
runner-upgrade repair --runner 1
runner-upgrade run --runner 1 --yes
runner-upgrade check --all
```

`repair` requires a fresh registration token for that runner's original scope;
see the deleted-registration recovery section below.

### What `run` does

1. Check for the known deleted-registration error and active local jobs before
   changing services. A deleted registration must be repaired first.
2. Resolve the latest GitHub runner release. Download and verify its published
   SHA-256 before stopping any selected runner. No download is needed when all
   selected binaries are current. Existing `.prev` backups block another binary
   upgrade until the operator resolves them.
3. Mark and stop every selected runner, confirming its LaunchDaemon unloads.
   A shutdown failure stops the operation before any binary upgrade begins.
4. Upgrade the first outdated runner as a canary. Retain registration and
   migration files, `.path`, `.env`, `.service`, `.github_pat`, and `_work`.
   Prepare launchd log paths and the executable service wrapper before startup.
5. Wait for readiness from the fresh diagnostic logs. The default timeout is
   **300 seconds**, allowing GitHub's four-minute session-conflict retry window;
   override it with `--health-timeout SECONDS`. Concurrent `--version` logs do
   not hide the service's readiness message. The explicit deleted-registration
   error fails immediately and prints the repair command.
6. After a healthy canary, upgrade and start the remaining runners. Selected
   runners whose binaries are already current are also restarted. If any start
   or upgrade fails, stop the flow; unstarted runners remain in maintenance.

If a binary upgrade fails, the helper attempts rollback of that runner. Read
its recovery status: `ROLLED BACK (unconfirmed)` means the old files were
restored but startup was not confirmed. Other selected runners remain stopped
until explicitly resumed. Use `start --runner N` to resume one without restarting
an already running service. `start` does not repair a deleted registration.

### Watchdog and autoscaler coordination

Shutdown writes `/opt/github-runners/.maintenance/runner-N`. The v1.9.0 health
watchdog skips marked runners, and the autoscaler holds while any runner is
marked. Successful startup clears the runner's marker; failures leave it in
place so maintenance is not undone by a controller.

Restart any long-running watchdog or autoscaler after upgrading the Homebrew
tools and before maintenance. An older process still executing v1.8.0 code does
not know about these markers. A fresh launchd timer invocation reads the new
script. These local markers do not pause GitHub job dispatch or coordinate
other machines. Do not run overlapping maintenance commands on the same host.

## Deleted-registration recovery

This startup error is a registration failure, not a binary-version failure:

```text
The runner registration has been deleted from the server, please re-configure.
```

The listener can connect to GitHub but cannot create a session with its obsolete
identity. Restoring older binaries retains the same invalid credentials. A
plain `runner-setup` rerun is not the recovery procedure: an already loaded
service may be skipped, and configuration refuses existing local registration.

Run repair as the runner owner:

```sh
runner-upgrade repair --runner 1
```

The helper displays the saved runner name and GitHub URL. Get a fresh
**registration token** from that scope's GitHub **Settings → Actions → Runners
→ New self-hosted runner** page and enter it at the local configuration prompt.
This is not a removal token. Configuration also asks for custom labels if you
did not specify `--labels`; restore the labels your workflows expect. Labels
are not saved in `.runner`. The setup tool's default custom labels were
`macos,macmini,self-hosted`, but existing fleets may have used other labels.

Repair targets one runner. It:

- Backs up registration, migration state, and operator settings privately under
  `/opt/github-runners/.registration-backups/`.
- Stops the selected service and uses the runner's native `remove --local` to
  clear obsolete local registration. It makes no server deletion request.
- Reconfigures the existing binary with its saved URL, name, work directory,
  runner group, ephemeral setting, and automatic-update setting. `--runnergroup`
  can override the saved group. `.env`, `.path`, `_work`, and the plist are kept.
- Avoids `--replace`, so a same-name runner on another machine is not silently
  displaced. Resolve a reported name collision before retrying.
- Starts the existing service and verifies readiness. If registration fails,
  the runner stays stopped with its workspace and diagnostics retained. Retry
  uses the protected backup metadata without restoring obsolete credentials.

For unattended repair, supply `RUNNER_TOKEN` and explicit `--labels`:

```sh
RUNNER_TOKEN=$(runner-token registration --org YOUR_ORG) \
  runner-upgrade repair --runner 1 --labels macos,macmini,self-hosted --yes
```

Use the runner's actual organization or repository scope with `runner-token`;
its authorizing credential must already be configured. The helper does not log
or persist the fresh token. Upstream configuration accepts a supplied token as
a process argument, so prefer the interactive token prompt on shared hosts.

After repair succeeds, use `run --runner 1` to upgrade that runner's binaries.
Custom labels and a fresh token cannot be reconstructed from a deleted server
registration; the operator must provide them.

### Choose an organization instead of the saved repository

Repair normally reuses the old registration scope. In v1.9.1, select a different
scope explicitly when moving an existing runner:

```sh
runner-upgrade repair --runner 1 --org lev7finance
# Or choose one repository:
runner-upgrade repair --runner 1 --repo OWNER/REPO
```

Use a fresh registration token from the **same scope printed by repair**. For
`--org lev7finance`, open the organization's [New self-hosted runner page](https://github.com/organizations/lev7finance/settings/actions/runners/new).
An organization token cannot register the runner with a saved repository URL
such as `https://github.com/bloxwap/monorepo`; that mismatch can produce GitHub's
`404 Not Found` during authentication. An expired token can also produce 404.
The URL shown in the GitHub page's `config.sh --url ...` command must match
repair's target URL. Enter its registration token at the local prompt.

The new scope is saved for retries if authentication fails. The original
registration stays in the private backup. A scope change does not carry the
old runner group into the new scope; choose a destination group when prompted,
or specify `--runnergroup NAME`. Name, work directory, and lifecycle settings
are preserved. The helper does not delete any old server registration or
replace an existing registration on another machine.

For a service using `runner-ephemeral`, repair also updates the supervisor's
saved scope and labels in its LaunchDaemon. Supply `--labels` explicitly so
subsequent jobs retain those labels. Its PAT (from the environment or configured
PAT file) must also authorize registration in the destination organization;
the temporary registration token does not grant that ongoing access. The
supervisor preserves the configured runner group across job cycles.

### Which repositories can use an organization runner?

An organization runner serves repositories allowed by its runner group. In
GitHub, open **Organization Settings → Actions → Runner groups**, select the
group, and configure **Repository access** for all or selected repositories.
Their workflows can then request its labels, for example:

```yaml
runs-on: [self-hosted, macOS, ARM64]
```

A repository registration serves one repository; an organization registration
serves allowed repositories within that organization. Enterprise registrations
can be assigned to multiple organizations in the same enterprise. A single
registration cannot serve arbitrary unrelated organizations. One Mac can host
separate runner instances registered to different scopes, sharing its hardware
capacity. See GitHub's [runner scopes](https://docs.github.com/en/actions/concepts/runners/self-hosted-runners)
and [runner-group access controls](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access).

## Rollback and retained diagnostics

```sh
runner-upgrade rollback --runner 2
runner-upgrade rollback --all --yes
```

Rollback confirms the service stopped, moves the current `_work` back into
`runner-N.prev`, and restores that installation. If both directories contain
`_work`, it refuses to overwrite either. Startup diagnostics are archived under
`/opt/github-runners/.upgrade-diagnostics/` before any failed installation is
removed. Prior diagnostics are archived before starting the restored version,
so old readiness text cannot make an unconfirmed rollback look healthy.

Read both `runner-stdout.log` and `runner-stderr.log`, plus `Runner_*.log` in the
printed archive. GitHub's service supervisor sends listener stderr to service
stdout. The stable event log is `/opt/github-runners/upgrade.log`.

Once satisfied, remove `.prev` directories to reclaim space. Retain/export
needed diagnostics and registration backups before applying your own retention
policy. Registration backups contain obsolete credentials and must remain
private. The helper does not expire these files automatically.

## Scheduling version checks

Schedule `check` separately from the maintenance window. For example, save this
as `/Library/LaunchDaemons/com.github.runner-upgrade.plist`, owned by `root:wheel`
and mode `644`. Replace the owner and Homebrew path for the host:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.github.runner-upgrade</string>
  <key>UserName</key><string>joe</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/runner-upgrade</string>
    <string>check</string><string>--all</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>HOME</key><string>/Users/joe</string></dict>
  <key>StartInterval</key><integer>21600</integer>
  <key>StandardOutPath</key><string>/opt/github-runners/upgrade-launchd-stdout.log</string>
  <key>StandardErrorPath</key><string>/opt/github-runners/upgrade-launchd-stderr.log</string>
</dict>
</plist>
```

The runner owner must be able to write the output paths. `check` exits 3 when
upgrades are pending, 0 when versions are current, and 1 on errors.
Unattended maintenance additionally requires `--yes`, the appropriate sudo
configuration, and an external process that pauses dispatch and drains jobs.
It must run as the runner owner; runner processes cannot run as root.
