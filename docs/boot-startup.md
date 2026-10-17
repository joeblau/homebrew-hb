# Runner power settings and boot verification

`runner-setup` prepares a Mac for always-on CI before provisioning its services.
It applies these persistent settings to all power sources where the hardware
supports them, then reads them back with `pmset -g custom`:

| Setting | Value | Effect |
| --- | --- | --- |
| `autorestart` | `1` | Restart when power returns after a power failure |
| `sleep` | `0` | Disable automatic system sleep |
| `disksleep` | `0` | Disable automatic disk sleep |
| `powernap` | `0` | Disable Power Nap |

Unsupported settings are reported and skipped. A failed write or verification
stops setup before it provisions runners or Colima. Display sleep and screen
locking can remain enabled. Pass `--skip-power-config` to keep existing host
power settings, for example on a shared development Mac.

Inspect the resulting policy on each Mini:

```sh
pmset -g custom | grep -E 'autorestart|sleep|disksleep|powernap'
```

These settings persist across reboots; they do not need a separate LaunchDaemon.
For MDM-managed machines, deploy the equivalent power policy with MDM and use
`--skip-power-config` if that policy should remain under MDM control.

`autorestart=1` covers a power failure while the Mac was running. It does not
guarantee startup after a deliberate shutdown followed by reconnecting AC power.
That is a separate power-on policy. See [Apple's Energy settings reference](https://support.apple.com/guide/mac-help/mchlp1168/mac).

## The services installed by this tap

The existing service files are:

```text
/Library/LaunchDaemons/com.github.runner-1.plist
/Library/LaunchDaemons/com.github.runner-2.plist
/Library/LaunchDaemons/com.github.runner-colima.plist
```

The Colima service is installed when `--colima` is selected. Each plist is owned
by `root:wheel`, has mode `644`, enables `RunAtLoad`, and is loaded into launchd's
`system` domain. `UserName` selects the non-root runner owner. Explicit home and
PATH settings avoid depending on an interactive shell. The Colima service
supervises the foreground VM; each runner waits for Docker and Rosetta readiness.
The services have no Aqua login-session restriction.

Use these existing services when operating this tap. Adding a second generic
runner service can create competing listeners. A Homebrew user service for
Colima starts at login; it must not compete with this tap's Colima boot daemon.

## Verify one canary before updating the fleet

The isolated regression tests verify generated plists, permissions commands,
service domains and startup gates. A loaded daemon alone does not prove a
runner is ready. A real reboot with no GUI login is the acceptance check.

1. Choose an idle Mini. If remote access is needed, enable **System Settings →
   General → Sharing → Remote Login** for the service owner. Setup does not
   enable SSH or install Tailscale.
2. Check FileVault with `fdesetup status`. If it is enabled, its startup unlock
   must complete before services depending on the data volume can run. An
   ordinary logged-out desktop does not require automatic GUI login.
3. Restart the Mini, or perform a cold boot when that is the scenario being
   tested. Leave its GUI at the login window. Allow boot and Docker startup
   to complete, then verify both runners are **Idle** or **Active** in the
   organization's GitHub runners list before opening an SSH session. This
   proves they came online without any login triggering startup.
4. From another Mac, SSH into the Mini as its service owner and run the checks
   below to inspect the services. The console owner should not be
   the runner's GUI user. Each service should have `state = running`, and
   runner health should report readiness.

```sh
stat -f '%Su' /dev/console
sudo stat -f '%Su:%Sg %Lp %N' /Library/LaunchDaemons/com.github.runner-*.plist
sudo plutil -lint /Library/LaunchDaemons/com.github.runner-*.plist
sudo launchctl print system/com.github.runner-colima
sudo launchctl print system/com.github.runner-1
sudo launchctl print system/com.github.runner-2
runner-health --once --dry-run
docker --context colima info
colima ssh -- cat /proc/sys/fs/binfmt_misc/rosetta
```

The final Rosetta check applies to Apple Silicon Colima hosts and should start
with `enabled`. For hosts without Colima, omit its service and Docker checks.

If a runner remains in maintenance, resume it with `runner-upgrade start --all
--yes` and repeat the checks. Startup clears maintenance only after readiness.

If the services become ready only after GUI login and FileVault is off, capture
the existing boot failure before changing any registrations:

```sh
brew services list
tail -n 60 "$HOME/Library/Logs/runner-colima/stdout.log"
tail -n 60 "$HOME/Library/Logs/runner-colima/stderr.log"
tail -n 60 /opt/github-runners/runner-1/_diag/runner-stdout.log
tail -n 60 /opt/github-runners/runner-1/_diag/runner-stderr.log
sudo launchctl print-disabled system
```

These checks distinguish an unloaded or disabled boot daemon from a VM,
network, or runner readiness failure. Power settings cannot resolve a service
that depends on a GUI login; its boot error still needs to be diagnosed.
