# Cleanup that keeps runners warm

`runner-prune` removes checkout/build output from one runner after its job.
It preserves `_work/_tool`, `_work/_actions`, `_work/_temp`,
`_work/_PipelineMapping`, and shared package caches by default. A job finishing
on runner-1 must never delete runner-2's active checkout or shared compiler
and package caches.

```sh
# Inside a job-completed hook: RUNNER_TEMP identifies this runner.
runner-prune --min-free-gb 20

# Manually, after the selected runner is idle/stopped:
runner-prune --runner-dir /opt/github-runners/runner-1 --dry-run

# Maintenance only: stop ALL runners and other cache consumers first.
runner-prune --all --offline --purge-caches
```

`--offline` acknowledges that you have stopped every consumer. The script does
not stop services or prove that a machine is idle. Fleet cleanup, shared-cache
eviction, and `RUNNER_PRUNE_DOCKER=1` require `--all --offline` and are rejected
inside jobs. Do not schedule these flags against a running fleet. The default
unscoped command fails outside a hook; old fleet-wide wrappers must be updated.

## Post-job hook

Create a wrapper outside `_work`, adjusting the executable path for Intel
Homebrew (`/usr/local/bin`) if necessary:

```sh
sudo mkdir -p /opt/github-runners/hooks
sudo tee /opt/github-runners/hooks/job-completed.sh >/dev/null <<'HOOK'
#!/bin/bash
exec /opt/homebrew/bin/runner-prune --min-free-gb 20
HOOK
sudo chmod +x /opt/github-runners/hooks/job-completed.sh
```

Set the following in each runner LaunchDaemon's `EnvironmentVariables` dict:

```xml
<key>ACTIONS_RUNNER_HOOK_JOB_COMPLETED</key>
<string>/opt/github-runners/hooks/job-completed.sh</string>
```

Drain the runner before reloading its service:

```sh
sudo launchctl bootout system/com.github.runner-1
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-1.plist
```

GitHub executes hooks synchronously, supplies the job's environment, and
includes hook output in the workflow logs. Keep cleanup bounded because it
adds to job time; hook timeout handling must be implemented by the hook itself.
See [GitHub's job hook documentation](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts).

## Disk limits and cache maintenance

| Exit | Meaning |
| --- | --- |
| 0 | Cleanup completed, or dry-run completed. |
| 1 | An operation failed or a path was unsafe. |
| 2 | Invalid arguments or missing offline acknowledgement. |
| 75 | Free disk remains below the requested GiB floor. |

Exit 75 reports low disk; it does **not** prevent the runner accepting its
next job. Have the supervisor/operator drain and stop the service when this
signal occurs. A failing post-job hook can affect the job result; do not
assume the job is already immune to failure at this point.

Measure cache growth and use tool-specific size/age limits where supported.
`--purge-caches` is an explicit cold-cache reset for Homebrew, npm, Yarn, pip,
SwiftPM, Gradle, and Maven. It removes whole caches only during the acknowledged
maintenance window. Repeated eviction after every job defeats their benefit.
Docker pruning removes unused data on the currently selected Docker daemon;
remote BuildKit cache retention needs its own policy on the builder host.

The script refuses symlinked runner/work roots and verifies that a hook's
selected directory matches `RUNNER_TEMP`. Runner user directories still share
an operating-system account; these checks are not isolation from malicious jobs.
