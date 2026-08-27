# Log aggregation for self-hosted runners

`runner-logs` ships the logs and metrics of the GitHub Actions runners that
`runner-setup` provisions (under `/opt/github-runners`) to one central,
searchable sink. It has two modes:

- **`runner-logs ship`** — tails every `runner-*/_diag/*.log` (the runner's
  `Runner_*.log` and `Worker_*.log` files, plus the `runner-stdout.log` /
  `runner-stderr.log` that the LaunchDaemon plist captures) and forwards new
  lines as JSON. Byte offsets are persisted in a state file
  (`~/.runner-logs/offsets.tsv` by default), so restarts resume where they
  stopped; rotated or truncated files are detected via inode+size and re-read
  from the top. Delivery is at-least-once: if a sink write fails, offsets are
  not advanced and those lines are re-sent on the next pass.
- **`runner-logs metrics`** — emits one JSON line per runner per interval to
  stdout (see the schema below).

No `jq` dependency, bash 3.2 compatible. Ship mode runs read-only against
`/opt` and needs **no sudo**; metrics mode uses sudo only for
`launchctl print system/...`.

## Quick start

```sh
# One-off ship to local JSON-lines files (zero dependencies):
runner-logs ship --once
ls ~/Library/Logs/github-runners/

# Continuous ship to an HTTP collector (Fluent Bit / Logstash / Vector HTTP
# input in front of Elasticsearch or Loki):
export LOG_HTTP_ENDPOINT="https://logs.example.com:8080/runner-logs"
export LOG_HTTP_TOKEN="..."        # sent as Bearer, via a 0600 curl config
runner-logs ship --sink http --interval 15

# One metrics snapshot per runner:
runner-logs metrics --once

# ... including GitHub queue depth (PAT with actions:read):
GITHUB_PAT=ghp_... GITHUB_QUEUE_REPO=acme/monorepo runner-logs metrics --once
```

## Log line schema (ship mode)

One JSON object per shipped line:

```json
{"ts":"2026-08-27T20:30:00Z","host":"ci-mac-1","runner":"runner-2","source":"Worker_20260827-201200-utc.log","line":"[2026-08-27 20:29:58Z INFO JobServerQueue] Job 'build' completed with result: Succeeded","job_name":"build","job_result":"Succeeded","job_duration_s":780}
```

- `ts` — when the shipper read the line (UTC).
- `host` — sanitized `LocalHostName`, matching the runner name prefix that
  `runner-setup` uses.
- `runner` — local runner directory name (`runner-N`).
- `source` — basename of the log file the line came from.
- `line` — the raw log line.
- `job_name` / `job_result` / `job_duration_s` — present **only** on Worker
  "completed with result" lines. `job_duration_s` is derived from the
  `Worker_YYYYMMDD-HHMMSS-utc.log` filename (job start, UTC), so dashboards
  can graph success/failure rate and job duration without any parsing.

## Metrics schema (metrics mode)

One JSON object per runner per interval, on stdout:

```json
{"ts":"2026-08-27T20:30:00Z","host":"ci-mac-1","runner":"runner-1","runner_name":"ci-mac-1-runner-1","daemon_loaded":true,"status":"idle","last_job_name":"build","last_job_result":"Succeeded","disk_total_kb":499963174912,"disk_used_kb":234567890,"disk_avail_kb":499728607022,"disk_used_pct":1,"queue_queued":3,"queue_in_progress":2}
```

- `status` — `offline` (LaunchDaemon not loaded), `idle` (newest diag log's
  last marker is `Listening for Jobs`), `busy` (last marker is
  `Running job`), or `unknown`.
- `last_job_result` — from the newest Worker log's last
  `completed with result:` line (`Succeeded`, `Failed`, `Cancelled`, ...).
- `disk_*` — `df -k` on the runner volume.
- `queue_queued` / `queue_in_progress` — only when `GITHUB_PAT` is set. The
  repo slug comes from `GITHUB_QUEUE_REPO`, or from a repo-scoped runner's
  `.runner` `gitHubUrl`. Org-scoped runners need `GITHUB_QUEUE_REPO`.

Pipe metrics into the same pipeline, e.g.:

```sh
runner-logs metrics --interval 60 >> ~/Library/Logs/github-runners/runner-metrics.jsonl
```

or run it under its own `StartInterval` LaunchDaemon and ship the resulting
file with a second `runner-logs ship --output-dir`-style collector.

## Running under launchd

A sample plist ships in
[`docs/com.github.runner-logs.sample.plist`](com.github.runner-logs.sample.plist).
It runs `runner-logs ship --sink http --once` every 30 s (`StartInterval`),
which pairs naturally with the offset state file — each run ships only what is
new. Because `--once` + `StartInterval` is used instead of a long-running
daemon, config changes (endpoint, token) take effect on the next tick simply
by editing the plist.

```sh
sudo cp docs/com.github.runner-logs.sample.plist \
  /Library/LaunchDaemons/com.github.runner-logs.plist
sudo chown root:wheel /Library/LaunchDaemons/com.github.runner-logs.plist
sudo chmod 644 /Library/LaunchDaemons/com.github.runner-logs.plist
# edit LOG_HTTP_ENDPOINT / LOG_HTTP_TOKEN / program path first
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.runner-logs.plist
sudo launchctl kickstart -k system/com.github.runner-logs   # run once now
```

The sample runs as root purely so every runner's `_diag` is readable
regardless of ownership; `runner-logs` never starts/stops runners and ship
mode invokes no sudo. For **metrics** under launchd as a non-root user, grant
passwordless sudo for the read-only launchctl query:

```
youruser ALL=(root) NOPASSWD: /bin/launchctl print system/com.github.runner-*
```

## Sinks

### `--sink file` (default)

Appends to `~/Library/Logs/github-runners/runner-logs-YYYYMMDD.jsonl`
(override with `--output-dir`). Zero dependencies — point Promtail, Fluent
Bit, Filebeat, or Vector at that directory.

### `--sink http`

POSTs newline-delimited JSON batches (`Content-Type: application/x-ndjson`,
`--batch-size`, default 500 lines) to `LOG_HTTP_ENDPOINT`, with
`Authorization: Bearer $LOG_HTTP_TOKEN` when set. The token is handed to curl
through a temporary 0600 `-K` config file so it never appears in `ps` or logs.

Elasticsearch's `_bulk` and Loki's push API have their own wire formats, so
terminate the HTTP POST in a small collector that fans out — examples below.

## Aggregator setup + sample queries

### Loki + Grafana (via Fluent Bit)

Fluent Bit config (`fluent-bit.conf`) receiving the HTTP sink:

```ini
[INPUT]
    Name   http
    Listen 0.0.0.0
    Port   8080

[OUTPUT]
    Name   loki
    Match  *
    Host   loki.example.com
    Port   3100
    Labels job=github-runners
```

LogQL queries (Grafana Explore, `{job="github-runners"}`):

- **All logs for one runner:** `{job="github-runners"} | json | runner="runner-1"`
- **Errors only:** `{job="github-runners"} |~ "(?i)error|failed"`
- **Success / failure rate:**
  ```
  sum(count_over_time({job="github-runners"} | json | job_result="Succeeded" [$__interval]))
    /
  sum(count_over_time({job="github-runners"} | json | job_result!="" [$__interval]))
  ```
- **Job duration p95 per runner:**
  ```
  quantile_over_time(0.95,
    {job="github-runners"} | json | job_duration_s != ""
    | unwrap job_duration_s [$__interval]) by (runner)
  ```
- **Offline runners (metrics stream):**
  ```
  max_over_time({job="github-runners"} | json | unwrap daemon_loaded [10m]) == 0
  ```

Dashboard sketch: a *stat* panel with the success-rate ratio, a *time series*
with the p95 duration, a *table* of the newest metrics line per runner
(`status`, `disk_used_pct`), and an alert on `status="offline"` or
`disk_used_pct > 85`.

### Elasticsearch + Kibana (via Logstash)

Logstash pipeline:

```ruby
input {
  http {
    port => 8080
    codec => json_lines
  }
}
output {
  elasticsearch {
    hosts => ["https://es.example.com:9200"]
    index => "github-runners-%{+YYYY.MM.dd}"
  }
}
```

Then in Kibana (data view `github-runners-*`):

- **Success/failure rate:** Lens → formula
  `count(kql='job_result: "Succeeded"') / count(kql='job_result: *')`.
- **Job duration:** Lens → `average(job_duration_s)` (or p95 with
  `percentile`) broken down by `runner.keyword`.
- **Search a runner's logs:** KQL `runner: "runner-2" and line: *error*`.
- **Disk pressure alert:** KQL `disk_used_pct > 85` on the metrics documents.

## Operational notes

- **State file:** `~/.runner-logs/offsets.tsv` (override with
  `--state-file`). Delete it to re-ship everything. One entry per log file:
  path, inode, byte offset.
- **Rotation:** runner `_diag` files rotate per run/session; a changed inode
  or shrunken file resets that file's offset to 0, so nothing is skipped.
- **Duplicates:** delivery is at-least-once. After a sink outage the affected
  lines are re-sent; deduplicate downstream on `(runner, source, line)` if
  your store requires exactly-once semantics.
- **Cleanup:** `runner-cleanup` deletes `runner-*` directories; the shipper
  simply stops seeing those files. State entries for deleted files are dropped
  on the next pass.
