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
| `womp` | `1` | Wake on LAN (magic packet) |
| `tcpkeepalive` | `1` | Keep network sessions alive |

Unsupported settings are reported and skipped. A failed write or verification
stops setup before it provisions runners or Colima. Display sleep and screen
locking can remain enabled. Pass `--skip-power-config` to keep existing host
power settings, for example on a shared development Mac.

Inspect the resulting policy on each Mini:

```sh
pmset -g custom | grep -E 'autorestart|sleep|disksleep|powernap|womp|tcpkeepalive'
```

These settings persist across reboots; they do not need a separate LaunchDaemon.
For MDM-managed machines, deploy the equivalent power policy with MDM and use
`--skip-power-config` if that policy should remain under MDM control.

## Everything else setup does for unattended restarts

Each run of `runner-setup` also:

- **Upgrades itself first.** A Homebrew-installed `runner-setup` runs
  `brew upgrade joeblau/hb/runner-setup` and re-runs as the new release with the
  same arguments. Pass `--skip-self-upgrade` to use the installed release.
- **Turns off FileVault.** FileVault holds every restart, including
  `autorestart` after a power failure, at a password screen, so no runner can
  start. Setup runs `sudo fdesetup disable`, which asks for an admin's
  credentials once and decrypts in the background. The disk is then not
  encrypted at rest. Pass `--keep-filevault` to keep it and unlock each
  restart manually.
- **Installs Rosetta 2 on Apple Silicon Colima hosts**, accepting Apple's
  license, whenever x86_64 code cannot run. The check runs on every setup,
  including reruns that inherit Colima, because the runners refuse to listen
  without it. A Colima VM that started before Rosetta was installed is
  restarted between jobs so it comes back with Rosetta.
- **Turns on Remote Login (SSH)** so the fleet can be administered without a
  keyboard. macOS lets a terminal change this only with Full Disk Access
  (System Settings → Privacy & Security → Full Disk Access); without it setup
  prints the manual path and continues. After the first SSH connection, grant
  Full Disk Access to `sshd-keygen-wrapper` if remote sessions need protected
  directories. `--skip-remote-login` opts out.
- **Runs tailscaled as a system service.** The Tailscale menu-bar app only
  connects after GUI login, so a rebooted, logged-out Mini vanishes from the
  tailnet. Worse, current app releases do not persist their "Launch Tailscale
  at Login" setting across restarts
  ([tailscale/tailscale#18042](https://github.com/tailscale/tailscale/issues/18042)
  — the checkbox silently resets to disabled), so even a GUI login does not
  reliably reconnect the Mac; the issue's own workaround is the
  `TailscaleStartOnLogin` MDM key. The system service needs neither the app
  nor a login. Setup installs the Tailscale CLI and starts its Homebrew system
  service, which is reachable at the login window. The menu-bar app's login
  does not carry over — it can even answer `tailscale status` for the
  logged-in user and mask a logged-out boot daemon — so setup authenticates
  against the boot service's own socket:
  `sudo tailscale --socket=/var/run/tailscaled.socket up`, once,
  interactively. The boot service registers as its own tailnet node; remove
  the menu-bar app's old node in the Tailscale admin console, and quit the
  app itself — two tailscaled processes fight over the tunnel.
  `--skip-tailscale` opts out.
- **Installs the Tailscale panic guard.** macOS leaks one kernel interface
  reference for roughly every UDP packet tailscaled sends; the 32-bit count
  wraps at 2^32 and the kernel panics
  ([tailscale/tailscale#18953](https://github.com/tailscale/tailscale/issues/18953)).
  Setup installs `runner-tailscale-guard` as a root LaunchDaemon that
  accumulates the daemon's `magicsock_send_udp*` counters across tailscaled
  restarts (the kernel count survives them; only a reboot resets it) and
  cleanly reboots the host — waiting for idle runners first — long before the
  wrap. See "The tailscaled kernel panic, and the reboot guard" below.
  `--skip-tailscale` opts out of this too.
- **Starts every runner and waits until it listens for jobs.** Runners left in
  maintenance by an interrupted upgrade, and runners crash-looping on a
  readiness gate, are restarted. Setup fails if any runner does not come
  online, so a successful run means the runners are up.

GitHub's runner updates its own binary when a new release is required, so a
running fleet needs no scheduled upgrade job.

## The Rosetta boot race, and the boot-colima supervisor

Right after power-on, macOS can briefly report Rosetta as unavailable even
though it is installed. Observed on a Mini: 30 seconds after boot, Colima
logged `Unable to enable Rosetta: Rosetta2 is not installed` and silently
started its VM without Rosetta, while `arch -x86_64 /usr/bin/true` succeeded
minutes later. Every runner readiness gate then stays closed — `runs` climbs
in `launchctl print` with `last exit code = 1` — until the VM restarts.

The Colima boot daemon therefore runs `runner-docker-builder boot-colima`
instead of launching `colima start --foreground` directly. The supervisor
waits up to 180 s for host Rosetta, starts the VM, verifies
`/proc/sys/fs/binfmt_misc/rosetta` inside it, and on a mismatch stops the VM
and exits so launchd starts a fresh, correct one. Three failed attempts
(persisted in `~/Library/Logs/runner-colima/rosetta-restarts`) cap the loop;
the counter clears on the first healthy start. Setup reruns migrate older
direct-`colima` daemons to the supervisor between jobs.

## The tailscaled kernel panic, and the reboot guard

Running tailscaled for boot-time access exposes the fleet to an upstream
kernel bug,
[tailscale/tailscale#18953](https://github.com/tailscale/tailscale/issues/18953):
macOS's DLIL layer leaks one `ifnet` reference for roughly every UDP packet
tailscaled sends through its utun interface. When the 32-bit refcount
increments past 2^32 the kernel panics with
`dlil_if_ref: wraparound refcnt`, naming `tailscaled` (or Tailscale's network
extension) as the panicked task. It reproduces on Apple Silicon Macs across
macOS 26/27 and current Tailscale releases, with both the Homebrew tailscaled
and the standalone app. The leak rate tracks tailnet send volume, so time to
panic ranges from days (screen sharing or KVM streams over the tailnet) to
months (idle SSH access). Restarting tailscaled does **not** help: the leaked
references stay on the interface until the machine reboots.

Because this fleet is provisioned to come back from unattended reboots with
runners online and tailscaled connected, the mitigation is to turn the
eventual panic into a scheduled reboot. Setup installs
`/Library/LaunchDaemons/com.github.runner-tailscale-guard.plist`, which runs
`runner-tailscale-guard check` at boot and every 30 minutes as root. Each
pass reads `tailscale debug metrics` from the boot daemon's socket, sums the
`magicsock_send_udp*` counters, and accumulates them across tailscaled
restarts in `/var/db/runner-tailscale-guard/state`, keyed by the kernel boot
session UUID so a reboot resets the estimate. Past the soft limit
(2.0e9, half the wrap) it reboots as soon as no `Runner.Worker` process is
mid-job; past the hard limit (3.8e9) it reboots even mid-job, because the
panic would kill the job anyway and take the filesystem down uncleanly with
it. Guard logs go to `/Library/Logs/runner-tailscale-guard/`.

Inspect the estimate on a Mini at any time:

```sh
runner-tailscale-guard status
sudo runner-tailscale-guard check --dry-run
```

`autorestart=1` covers a power failure while the Mac was running. It does not
guarantee startup after a deliberate shutdown followed by reconnecting AC power.
That is a separate power-on policy. See [Apple's Energy settings reference](https://support.apple.com/guide/mac-help/mchlp1168/mac).

## The services installed by this tap

The existing service files are:

```text
/Library/LaunchDaemons/com.github.runner-1.plist
/Library/LaunchDaemons/com.github.runner-2.plist
/Library/LaunchDaemons/com.github.runner-colima.plist
/Library/LaunchDaemons/com.github.runner-tailscale-guard.plist
```

The Colima service is installed when `--colima` is selected; the Tailscale
guard whenever Tailscale setup is not skipped. Each plist is owned
by `root:wheel`, has mode `644`, enables `RunAtLoad`, and is loaded into launchd's
`system` domain. `UserName` selects the non-root runner owner. Explicit home and
PATH settings avoid depending on an interactive shell. The Colima service
supervises the foreground VM; each runner waits for Docker and Rosetta readiness.
The services have no Aqua login-session restriction.

Runner daemons use unconditional `KeepAlive`, so launchd restarts them after
both successful and unsuccessful exits. Maintenance uses `launchctl bootout`,
which unloads the job and therefore still stops it deliberately. Setup reruns
repair the startup policy on existing same-scope runners, including old Aqua
restrictions, without changing their registration or supervisor arguments.
They also remove a leftover official runner LaunchAgent even when the system
plist otherwise needs no changes.

Explicit `--colima` setup transfers the idle VM to system launchd: after
provisioning Docker, it stops the old supervisor and VM, then starts the VM in
the foreground through its system daemon. This handoff matters because
[`colima start --foreground` returns immediately when the VM is already running](https://github.com/abiosoft/colima/blob/main/cmd/start.go);
it does not adopt a VM started from a GUI session. The daemon gets 120 seconds
to shut down gracefully. Setup aborts if it cannot confirm the old daemon has
stopped or if stopping the VM fails.

Use these existing services when operating this tap. Adding a second generic
runner service can create competing listeners. A Homebrew user service for
Colima starts at login; it must not compete with this tap's Colima boot daemon.

## Apply the service update to an existing Mini

Pause job dispatch and let active jobs finish. Run as the runner owner on each
Mini; these commands use sudo internally:

```sh
runner-setup --org ORG_NAME --runners 2 --colima
```

Use the existing organization or repository and runner count for that machine.
For a brand-new Mini, install everything first with the tap's Brewfile:

```sh
brew tap joeblau/hb
brew bundle --file="$(brew --repository joeblau/hb)/Brewfile"
```

Existing same-scope Colima runners need no new registration token. Setup
upgrades the tools, repairs the services, resumes held runners and returns
only after every runner is listening for jobs. Deleted GitHub registrations
need registration repair separately; a service-policy update cannot restore
them.

## Verify one canary before updating the fleet

The isolated regression tests verify generated plists, permissions commands,
service domains and startup gates. A loaded daemon alone does not prove a
runner is ready. A real reboot with no GUI login is the acceptance check.

1. Choose an idle Mini. Setup enables Remote Login (SSH) itself when the
   terminal has Full Disk Access; otherwise enable **System Settings →
   General → Sharing → Remote Login** for the service owner.
2. Run setup. Unless `--keep-filevault` was used, `fdesetup status` should
   report FileVault off or decrypting. An ordinary logged-out desktop does not
   require automatic GUI login.
3. Restart the Mini, or perform a cold boot when that is the scenario being
   tested. Leave its GUI at the login window. Allow boot and Docker startup
   to complete, then verify both runners are **Idle** or **Active** in the
   organization's GitHub runners list before opening an SSH session. This
   proves they came online without any login triggering startup.
4. From another Mac, SSH into the Mini as its service owner and run the checks
   below to inspect the services. The console owner should not be
   the runner's GUI user. Each service should have `state = running`, and
   runner health should report readiness.
5. Log into the GUI, then explicitly **Log Out**. Confirm both runners remain
   online in GitHub. Repeat with screen locking and display sleep. These are
   separate cases from a reboot and should not stop the system daemons.

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

If a runner remains in maintenance, rerun setup or `runner-upgrade start --all
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
