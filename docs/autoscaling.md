# Queue-aware autoscaling (`runner-autoscale`)

`runner-autoscale` scales the local macOS fleet between `--min` and `--max`.
It delegates registration, LaunchDaemons, and removal to `runner-setup` and
`runner-cleanup`. One tick is the default; `--daemon` repeats ticks.

```sh
# One repository; one matching queued job triggers scale-up by default.
GITHUB_PAT_FILE=/etc/github-runner-autoscale-pat \
  runner-autoscale --repo acme/monorepo --min 1 --max 4

# Organization registration: explicitly list EVERY repository this fleet serves.
GITHUB_PAT_FILE=/etc/github-runner-autoscale-pat \
  runner-autoscale --org acme \
    --queue-repo acme/api --queue-repo acme/web \
    --daemon --interval 120 --min 1 --max 4
```

## Queue observation

The controller uses GitHub's supported
[repository workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository)
and [workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run).
There is no organization-wide workflow runs endpoint. `--org` therefore
requires one or more `--queue-repo OWNER/REPO` arguments belonging to that
organization; `--repo` watches its own repository.

For each repository it lists queued **and in-progress** workflow runs, then
lists their latest-attempt jobs. This catches queued matrix siblings after a
workflow has already started. It counts queued jobs whose requested labels
are a subset of `--labels` plus the runner's default `self-hosted`, `macOS`,
and native architecture (`ARM64` or `X64`) labels, without case sensitivity.
Duplicate run/job IDs and repeated repository arguments are deduplicated.

All lists are paginated using `jq` to parse structured JSON. Failed requests,
malformed responses, or incomplete pagination prevent queue-based decisions.
GitHub limits filtered workflow searches to 1,000 results; a response beyond
that limit fails the tick instead of silently using a partial queue. Lists
are additionally bounded to 100 pages per endpoint.

## Decisions and lifecycle

Each tick takes a local `mkdir` lock, checks registration scope, observes the
queue, and makes at most one change:

- **Scale up:** below `--min`, or at least `--scale-up-threshold` matching jobs
  are queued (default **1**), provided the fleet is below `--max`. A new
  short-lived registration token is minted for this operation. The controller
  asks `runner-setup` for the first missing runner index, so an existing
  `runner-1, runner-3` fleet gets `runner-2` and cannot accidentally add two
  runners by treating directory count as the highest index.
- **Scale down:** the matching queue is empty, every local runner is confirmed
  online and idle, and the fleet has exceeded `--cooldown-minutes` of idle time
  (default **30**) while above `--min`. It mints a removal token and removes
  the highest-numbered runner. The cooldown resets after removal.
- **Hold:** when bounds, cooldown, or observed work do not justify a change.
  Missing, offline, malformed, or unregistered runner identities block removal.

Local `.runner` files must all belong to the configured scope. Mixed scopes
or incomplete registrations fail the tick; repair those before resuming.
Use `--ephemeral` with an initially ephemeral fleet to pass that mode and the
resolved PAT to `runner-setup` for every new runner. Omit it for persistent
runners. The controller checks each `.runner` mode and rejects mixed fleets.
Runner status is matched by the exact registered `agentName`, preventing a
busy `other-mac-runner-1` from being confused with this host's `runner-1`.
The runner list is paginated, including when the organization has more than
100 registered runners.

State and decisions live in `/opt/github-runners/.autoscale/`. Each log line
includes timestamp, matching queued-job count, current/min/max capacity,
action (`scale-up`, `scale-down`, `none`, `skip`, `error`), and reason. API
queue failures and failed setup/cleanup return nonzero; daemon mode retries
at the next interval. Failed runner-list queries hold capacity. Locks older
than one hour are treated as stale; operations should finish within that limit.

## Prerequisites and launchd

- Provision the initial fleet with `runner-setup` to create
  `/opt/github-runners` with the correct owner. Once initialized, `--min 0`
  can drain and later replenish this directory.
- Install the `runner-setup` formula, which supplies these commands and `jq`.
  For source checkouts, install `jq` and put both `runner-setup` and
  `runner-cleanup` on `PATH`.
- Run as the same non-root user that owns the runners.
- Set `GITHUB_PAT` or a readable `GITHUB_PAT_FILE`. For classic PATs, private
  repository queue reads require `repo`; organization runner management also
  requires `admin:org`. Fine-grained credentials need repository Actions read
  plus repository Administration write or organization Self-hosted runners
  write for the chosen registration scope. Grant repository read access to
  every `--queue-repo`.

Keep the PAT in a mode-0600 file, outside the world-readable LaunchDaemon
plist. The controller passes authentication to curl through a mode-0600
configuration file and uses environment variables for registration/removal
tokens. It does not print tokens.

Example `/Library/LaunchDaemons/com.github.runner-autoscale.plist` (replace
`joe`, repository names, and paths to match your installation):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.github.runner-autoscale</string>
  <key>UserName</key><string>joe</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/runner-autoscale</string>
    <string>--org</string><string>acme</string>
    <string>--queue-repo</string><string>acme/monorepo</string>
    <string>--max</string><string>4</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GITHUB_PAT_FILE</key><string>/etc/github-runner-autoscale-pat</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/opt/github-runners/.autoscale/launchd-stdout.log</string>
  <key>StandardErrorPath</key><string>/opt/github-runners/.autoscale/launchd-stderr.log</string>
</dict>
</plist>
```

```sh
sudo chown root:wheel /Library/LaunchDaemons/com.github.runner-autoscale.plist
sudo chmod 644 /Library/LaunchDaemons/com.github.runner-autoscale.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-autoscale.plist
```

`runner-setup` and `runner-cleanup` perform privileged filesystem and launchd
operations using sudo. Unattended operation needs an administrator-configured
command policy for these operations; verify a complete tick without an
interactive password prompt before enabling the daemon. Blanket passwordless
access to `rm`, `cp`, or `chown` grants broad root write access and is not a
read-only permission. A cached interactive sudo session is temporary.

## Capacity, API budget, and limits

Start with a warm minimum and size `--max` from measured CPU, memory, and disk
pressure. Each extra runner is another process on the same Mac; it does not
add hardware or reserve CPU/RAM for a job. Scale-up creates at most one runner
per tick, including when restoring the minimum. Registration and provisioning
latency remain in addition to the poll interval.

On an empty queue a tick uses two run-list requests per watched repository,
plus the runner-list pages. Add a jobs request for each active workflow run,
extra requests for all additional pages, and a token request for a scaling
event. For `R` repositories with no runs and one runner-list page, the idle
budget is approximately `(3600 / interval) × (2R + 1)` requests/hour.
Busy matrix workflows can cost substantially more; select the interval and
repository list to fit the token's actual GitHub rate limit. HTTP failures
hold capacity; the controller does not infer that a rate-limited queue is empty.

Use one controller per scope and give the local root a single registration
scope. There is no cross-host capacity reconciliation or distributed lock.
Other Macs can run fixed fleets; disable the primary before enabling a
standby controller. If runners can serve an unwatched repository, that work
will not trigger scale-up; configure every eligible repository explicitly.

Label matching cannot infer runner-group repository access policy, workflow
concurrency restrictions, or approvals. Those can keep jobs waiting even
when capacity exists. Scaling uses queued jobs in queued/in-progress runs;
it does not attempt to provision for workflows blocked on approvals or
workflow concurrency. Ephemeral re-registration gaps can delay a tick until
every runner has completed registration again.

Scale-down uses an API observation, not an atomic drain operation. GitHub can
assign a new job between the idle check and runner removal. Fleets requiring
guaranteed uninterrupted jobs should keep `--min` equal to `--max` until a
job-aware drain/supervisor is available. Directory-based ephemeral runners
also expose re-registration gaps, which this controller treats as unknown.
