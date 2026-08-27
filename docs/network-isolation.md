# Network isolation & VPN kill-switch for self-hosted runners

`runner-netisolate` restricts the **outbound** network traffic of the macOS user
that your GitHub Actions runners run as, using a managed
[pf(4)](https://man.freebsd.org/cgi/man.cgi?pf) anchor (`com.github-runners`).
It exists for one scenario: you run **sensitive jobs** on a self-hosted Mac and
want a second layer of assurance that a compromised or malicious workflow can
only talk to the destinations you approved — and cannot exfiltrate data to
arbitrary hosts.

```
runner-netisolate install [--user USER] [--allow CIDR ...] [--vpn-interface utunX] [--yes]
runner-netisolate allowlist [--allow CIDR ...]
runner-netisolate status
runner-netisolate uninstall [--yes]
```

## What the policy allows (default mode)

Outbound from the target user, first matching rule wins; everything else is
**blocked and logged** to `pflog0`:

1. Loopback (`lo0`) — local tool caches, runtimes, proxies.
2. DNS (tcp/udp 53) to the machine's configured resolvers only (detected at
   install time via `scutil --dns` + `/etc/resolv.conf`).
3. NTP (udp 123) and DHCP (udp 67–68).
4. TCP to GitHub's published endpoint ranges, fetched **at install time** from
   <https://api.github.com/meta> (`web`, `api`, `git`, `packages`, `actions`,
   and `containers` if present).
5. TCP to a small set of fixed GitHub FQDNs the runner protocol needs
   (`github.com`, `api.github.com`, `codeload.github.com`, `ghcr.io`,
   `pipelines.actions.githubusercontent.com`,
   `results-receiver.actions.githubusercontent.com`,
   `objects.githubusercontent.com`, `pkg-containers.githubusercontent.com`,
   `github-cloud.s3.amazonaws.com`,
   `github-production-release-asset-2e65be.s3.amazonaws.com`).
6. Your `--allow` CIDR list — internal endpoints jobs legitimately need
   (artifact mirrors, proxies, internal SCM). Manage it later with
   `runner-netisolate allowlist --allow ...`, which reloads **only** the anchor.
7. Everything else: `block out log quick` — denied and logged.

Inbound is untouched; the runners only make outbound connections anyway.

## VPN kill-switch mode (fail closed)

```
runner-netisolate install --vpn-interface utun5
```

This replaces the endpoint policy with exactly two passes for the target user:
loopback, and the tunnel interface. There is no route around it: every packet
that is not on `utun5` hits the logged default deny. **If the tunnel drops, all
runner egress stops** — the failure mode is closed, never "fall back to the
physical interface". Combine with routing the tunnel to a trusted egress so
GitHub is reachable *through* the VPN.

Notes:

- `--allow` is rejected in this mode (nothing may bypass the tunnel).
- If the interface doesn't exist at install time you get a warning, not an
  error — the kill-switch simply blocks until the tunnel comes up.
- Verify the interface name with `ifconfig -l` / your VPN client before
  installing; `utun` numbering is not stable across reboots.

## Safety guarantees (how pf.conf is handled)

- The original `/etc/pf.conf` is backed up **once** to
  `/etc/pf.conf.runner-netisolate.bak` on first install.
- The anchor is referenced through a clearly marked block
  (`# >>> runner-netisolate ... >>>` / `# <<< ... <<<`) appended to `pf.conf`;
  uninstall removes exactly that block.
- **Every** rewritten config is parse-checked with `pfctl -n -f` *before* it
  replaces the real file; a config that fails verification is never written or
  loaded. The anchor itself is verified before installation too.
- Reloads are atomic (`pfctl -f` swaps the ruleset); `allowlist` reloads only
  the anchor and leaves the rest of the ruleset alone.
- `uninstall` removes the anchor, its tables, the `pf.conf` reference, reloads
  the restored config, and re-disables pf if it was disabled before install
  (recorded in `/etc/pf.anchors/com.github-runners.state`). The backup file is
  deliberately kept.

## Limitations of pf user-matching — read before relying on this

- **Per-user, not per-process.** Every process of the target user is
  restricted. On a Mac you also interactively use, *your* browsing and tooling
  break too. Use a dedicated runner machine or a dedicated `--user` account.
- **Host-level only.** pf cannot stop a job that escalates to another user,
  tampers with `pfctl` (root), or rides over allowed destinations (e.g.
  exfiltration *through* github.com via issues/gists). It is a containment
  layer, not a sandbox.
- **Hostnames resolve once, at load time.** pf has no dynamic DNS tracking and
  **cannot match wildcards**: `*.actions.githubusercontent.com` is represented
  only by its fixed members in the FQDN table. GitHub documents these hosts as
  required; their IPs drift. Re-run `runner-netisolate install` on a schedule
  (e.g. daily via cron/launchd) to re-resolve and refresh the meta ranges.
- **No reboot persistence.** macOS does not re-enable pf at boot. After a
  reboot, re-run `runner-netisolate install`, or manage the
  `com.apple.pfctl` LaunchDaemon / a custom LaunchDaemon via MDM. Until then
  the machine is **unrestricted** — fail-open at boot is the price of not
  modifying system LaunchDaemons.
- **DHCPv6/IPv6 RA** are not covered by the infrastructure passes; if jobs need
  full IPv6, add the relevant link-local scopes via `--allow`.
- UDP to GitHub ranges is not permitted (the runner protocol is TCP/HTTPS);
  QUIC/HTTP-3 falls back to TCP.

## Stronger isolation: segment the network, not just the host

pf user-matching is the *weak* layer. If the threat model includes a hostile
job with kernel/root ambitions, put the runner on its own network and enforce
egress where the job cannot reach the controls:

- **Dedicated VLAN or SSID** for runner machines, with the firewall/router
  permitting only the GitHub ranges above (your edge device can also do
  FQDN-based egress rules, which pf cannot).
- **Physical segmentation** (separate Mac, separate switch port) for the
  highest-sensitivity jobs.
- An **explicit egress proxy** (Squid/mitmproxy) on the segment as the only
  allowed next hop — gives you per-domain auditing pf lacks.
- MDM-enforced pf enablement at boot to close the fail-open window.

Then run `runner-netisolate` as defense-in-depth on top.

## Auditing

```
runner-netisolate status                                   # files + live rules + table sizes
sudo pfctl -s rules  -a com.github-runners                 # live anchor rules
sudo pfctl -s labels -a com.github-runners                 # per-rule hit counters (rni-*)
sudo pfctl -a com.github-runners -t gh_meta  -T show       # GitHub ranges in force
sudo pfctl -a com.github-runners -t gh_allow -T show       # your allowlist in force
sudo tcpdump -ni pflog0                                    # blocked packets, live
sudo tcpdump -ni pflog0 host 203.0.113.9                   # "who tried to reach X?"
cat /etc/pf.anchors/com.github-runners.gh_allow            # on-disk policy inputs
diff /etc/pf.conf.runner-netisolate.bak /etc/pf.conf       # exactly what was added
```

The `rni-*` rule labels make per-rule counters available via `pfctl -s labels`,
so you can see *which* rule is absorbing traffic (e.g. a spike in `rni-deny`
after a new workflow means it wants something you haven't allowed).

## Review checklist (run before trusting a sensitive job to this)

- [ ] `runner-netisolate status` shows the anchor installed, referenced, and
      live, with the intended target user.
- [ ] `sudo pfctl -s rules -a com.github-runners` ends in
      `block out log quick ... user <runner>` and every pass above it is one you
      recognize.
- [ ] `gh_meta`/`gh_fqdn` tables are fresh (`install` re-run within your drift
      window; hostname resolution is load-time only).
- [ ] `gh_allow` contains exactly the internal endpoints you approved — no
      broad `0.0.0.0/0` or `/8` shortcuts.
- [ ] From the runner user, a positive and a negative probe behave as expected:
      `curl -sI https://api.github.com` succeeds; `curl -sI https://example.com`
      fails and appears in `sudo tcpdump -ni pflog0`.
- [ ] Internal resources are reachable **only** via the intended path
      (e.g. through the proxy you allowlisted, not directly).
- [ ] Kill-switch mode (if used): disconnect the VPN and confirm runner-user
      egress stops entirely (`curl` hangs/blocks; `pflog0` shows denies on the
      physical interface).
- [ ] Reboot behavior is decided: `install` re-run automated, or MDM manages
      pf at boot — and the team knows enforcement is off until then.
- [ ] `diff /etc/pf.conf.runner-netisolate.bak /etc/pf.conf` shows only the
      marked `runner-netisolate` block.
- [ ] The runner machine is additionally segmented (VLAN/physical) if the
      threat model goes beyond accidental leakage.
