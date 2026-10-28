# Update runner Macs through Mosyle

The fleet-specific wrapper is kept locally at `scripts/mosyle-runner-update.sh`
and ignored by Git, along with its local regression tests at
`tests/test_mosyle_runner_update.py`. These files are not included in a fresh
clone or installed by the tap.

Paste the complete contents of the local wrapper into a Mosyle **Custom
Command** and configure it to **run as root**. Keep the
`#!/bin/bash` first line. Assign it to an existing runner Mac first, then to
the remaining runner devices after that Mac passes. Allow enough execution
time for Homebrew downloads and package upgrades.

Pause GitHub job dispatch and let active jobs finish before assigning the
command. The script refuses to proceed while a `Runner.Worker` is running;
this local check cannot prevent GitHub from assigning a new job afterward.

## What runs

1. Detect the native Homebrew installation (`/opt/homebrew` on Apple Silicon,
   `/usr/local` on Intel) and the runner service account from the numbered
   runner LaunchDaemons. If none exist, use the owner of the Homebrew executable.
   Mixed runner owners or an override that disagrees with the services fail.
2. Switch to that user's UID, home directory, and launchd context, then run
   `brew update`, `brew tap joeblau/hb`, and
   `brew upgrade joeblau/hb/runner-setup` (or install it if missing).
3. Run the updated, installed `runner-bootstrap`. This installs/upgrades the
   tap's Brewfile, applies the host settings, and prints its verification table.
4. Run `runner-health --once --dry-run` as root to check all discovered runners
   locally. Any command failure produces a nonzero exit status and a named
   failure stage in Mosyle's command output.

`brew update` updates Homebrew and package definitions; the targeted upgrade
and bootstrap's `brew bundle` update the runner tools and their dependencies.
See Homebrew's [update documentation](https://docs.brew.sh/FAQ#how-do-i-update-my-local-packages)
and [Bundle documentation](https://docs.brew.sh/Brew-Bundle-and-Brewfile).
Packages outside that dependency set are not upgraded. Automatic Homebrew
cleanup is disabled for this run so older files still used by services are
retained.

The wrapper uses the existing [bootstrap defaults](boot-startup.md), including
power policy, FileVault off, SSH, Tailscale, automatic login, keyboard navigation,
and Dock/desktop cleanup. It does not run `runner-setup`, change registrations,
upgrade the GitHub Actions runner binaries, or reboot. The health check reports
local state; verify connectivity in GitHub separately. Runners in maintenance
remain skipped by the health tool, and it reports when no runners exist.

## Account and privileges

Usually no edits are needed. For a nonstandard installation, edit the optional
variables near the top of the pasted script, for example:

```bash
RUNNER_USER="admin"
BREW_BIN="/opt/homebrew/bin/brew"
```

The account must be a local non-system user with an existing home directory
and access to its Homebrew installation. The script never relies on the
foreground console user and never changes Homebrew ownership. Missing
Homebrew requires the normal interactive bootstrap first.

Bootstrap requires sudo for system settings while running as the runner
owner. The wrapper installs a temporary, validated root-owned rule under
`/private/etc/sudoers.d/mosyle-runner-update-<pid>` granting that account
passwordless commands as root. It removes the rule on normal exit, errors,
and handled signals. The rule also expires one hour after creation, limiting
its lifetime after a forced kill or power loss. No account password or GitHub
token belongs in this script. The Mac's sudoers must include that directory;
the wrapper checks that the rule takes effect before running bootstrap.

## Read the result

Success ends with `SUCCESS: Homebrew updated; bootstrap and local runner
health checks passed.` On failure, inspect the named stage and bootstrap's
`FAIL` rows. The wrapper preserves required checks instead of skipping them
to obtain a successful exit. Bootstrap may still print nonfatal warnings;
for example, an unknown SSH state needs the management agent's Full Disk
Access permissions checked.

Automatic login setup and Tailscale authentication require an initial
interactive setup. FileVault decryption must finish before automatic login
can pass. If the GUI session is missing, arrange a login or an idle reboot
after automatic login is configured, then rerun the command. See the
[boot verification guide](boot-startup.md) for these prerequisites.

Concurrent executions are blocked by
`/private/var/run/mosyle-runner-update.lock`. If a forced kill leaves it behind,
first confirm the previous command and its child processes have stopped,
then remove that empty lock (or its staged `sudoers` file and directory) and
any leftover `mosyle-runner-update-<pid>` rule before retrying. The wrapper
does not automatically remove another invocation's lock or rules.
