# Audit of runner issues #52–#57

Audited local `main` at `f8ce28b` on 2026-09-21 against the pasted completion
claim and the current GitHub issue bodies. **The implementations are present,
but the claim that they are done correctly is not supported.** Findings below
include reproduced failures, not just missing staging evidence. This is an
audit; implementation files were not changed.

## Confirmed findings

### 1. P1 — Formula cannot install the three new tools from its declared source

`Formula/runner-setup.rb:7` still downloads `v1.10.0`, while its `bin.install`
list now requires `runner-bench`, `runner-sccache`, and `runner-ramscratch`.
None exists in that tag. `git ls-tree --name-only v1.10.0 runner-bench
runner-sccache runner-ramscratch` returns no entries. A fresh formula install
using this definition cannot satisfy its install list. Ruby syntax and
`--help` tests of the checkout do not verify packaging. Release an archive
containing the files and update the URL/checksum together before distribution.

### 2. P1 — RAM allocation identities collide and permit premature detach

`runner-ramscratch:512` encodes the ledger key as `${runner}__${job}`, but
underscores are valid in both fields. The distinct allocations `a__b/c` and
`a/b__c` therefore overwrite one ledger entry. Using the existing mocked
mount fixture, two 512 MB allocations produced `allocated_mb=512` and one
file named `a__b__c`. Cleaning the first invoked `hdiutil detach /dev/disk9`
while the second job's directory still existed. A job between file accesses
need not hold an open file that prevents detach. Use an unambiguous identity
encoding or nested ledger directories, and test both colliding identities
and repeated allocations. The physical RAM volume remains bounded, but
reservation accounting and the active-allocation invariant are broken.

### 3. P1 — Scratch artifact retention fails on SSD fallback and copy errors

`runner-ephemeral:878` calls `copy_out_ram_scratch` only when
`RAM_SCRATCH_IS_RAM=1`. On SSD fallback, `_work/_scratch/_out` is deleted by
the next `wipe_state` without being copied to `_diag`. This was reproduced
with a temporary `_out/artifact`, teardown, and reset. In addition,
`copy_out_ram_scratch` at line 863 only warns when `cp` fails; teardown then
still cleans the RAM allocation. Copy outputs for both storage modes and
retain the allocation/source if copying fails. Tests need to cover failed
copy-out, not just successful RAM copy-out.

### 4. P1 — Failed benchmark workloads leave child processes running

`runner-bench:232` cleans the sampler and temporary metadata but not workload
descendants on normal failure. `kill_tree` is used by the interrupt handler,
not the failed-workload path. A real local run with
`--cmd 'sleep 60 & echo $! > child.pid; exit 1' --reps 1` returned exit 1
while the recorded child PID was still alive. The audit explicitly killed
that child afterward. Process ancestry alone is insufficient once the
parent exits and the child is reparented. Track workload process groups or
another durable ownership mechanism, and clean them on every exit path.

### 5. P1 — sccache server failure does not provide the advertised fallback

`runner-sccache:561` emits wrapper/cache settings without
`SCCACHE_IGNORE_SERVER_IO_ERROR=1`, while its help and
`docs/compiler-cache.md` promise an uncached build on server failure.
[Upstream sccache documentation](https://github.com/mozilla/sccache/blob/main/README.md)
explicitly says server communication failures fail builds by default and
names that opt-in variable. Emit and test the intended fallback setting,
and narrow the documentation to failure modes it actually handles. The
current tests mock statistics/commands; they do not prove compiler fallback.

### 6. P1 — Remote credentials are serialized as executable shell text

`runner-cache:546` writes raw input inside double quotes, and
`client_credentials_ok` later sources that file. Dollar signs, backticks,
quotes, and command substitutions are not escaped. A dummy secret
`dummy$UNSET_AUDIT_SECRET` round-tripped as `dummy` when the variable was
unset with nounset disabled; with nounset enabled it can fail validation.
Command-substitution text would execute when sourced. These were dummy
credentials in a temporary file; no production secrets were read. Store
credentials as data parsed without shell evaluation, or correctly encode
every literal value and validate round trips for shell metacharacters.

### 7. P2 — Persistent tool-cache validation does not enforce its boundary

`runner-ephemeral:709` checks a textual path prefix, without resolving
symlinks or `..`. A temporary `_toolcache -> _work/_tool` was accepted;
`wipe_state` then deleted the installed tool. The resolver also omits
`RUNNER_TOOLSDIRECTORY`, which precedes `AGENT_TOOLSDIRECTORY` in
[the GitHub runner's resolver](https://github.com/actions/runner/blob/main/src/Runner.Common/HostContext.cs).
The effective runner path can therefore differ from the one validated or
migrated. Resolve the effective configuration with upstream precedence,
validate its physical destination and ownership, and reject aliases into
disposable storage before resetting it. Existing tests prove the default
case, not this boundary.

### 8. P2 — Autoscaling does not fail closed on runner-list API failure

`runner-autoscale:680` sets `demand=1` after a failed runner-list request and
continues provisioning. Directly exercising `plan_scale_up_batch 8 2` with
`api_list` returning 1 produced batch `1` and status 0. If the minimum
deficit exceeds one, it can choose a larger batch despite its warning.
`test_runner_list_failure_limits_batch_to_one` explicitly asserts this
behavior. It contradicts both issue #55's fail-closed requirement and the
pasted summary. Unknown usable capacity should abort the scale-up decision;
the test needs to enforce that contract.

### 9. P2 — Concurrent benchmark records contain batch duration as job duration

`runner-bench:738` copies the repetition's start/end/duration into every job
record. A real two-slot run sleeping 1 second in slot 0 and 4 seconds in
slot 1 reported `duration_s=4` for both jobs and the host. It does not record
each job's completion time. This distorts per-job medians/tails and comparisons
under contention. Record start/end per slot and retain a separate host/batch
duration. Queue timing is also one user-supplied timestamp for the batch,
not an observed per-job queue event.

### 10. P2 — Concurrent slots are counted as independent repetitions

`runner-bench:1054` sets report `runs` to `len(jobs)` and uses that count for
the five-repetition warning around line 1125. One repetition at concurrency
5 is therefore presented as five samples and avoids the insufficient-
repetitions warning, although all share one host observation and, currently,
one duration. Report independent repetitions and job samples separately;
require five repetitions for the stated comparison gate.

### 11. P2 — The documented five-repetition benchmark is not repeatable

`docs/runner-performance.md:129` starts its command with `git clone ... src`,
but `runner-bench` reuses each slot's work directory. After a successful
first clone, the next repetition fails because `src` already exists. The
edited example uses the default `BENCH_EDIT.txt` in the slot root rather
than modifying a compiled source inside `src`; it does not demonstrate an
edited rebuild. Provide an executable workload that safely reuses or resets
the checkout, directs caches to the disposable path, and applies a valid
source edit at the appropriate point. Verify all five repetitions.

### 12. P2 — Remote path-style setting does not reach the advertised cache action

`runner-cache:2325` emits `RUNNER_CACHE_PATH_STYLE`, but the workflow snippet
never uses it. The selected action has no path-style input in its
[action manifest](https://github.com/tespkg/actions-cache/blob/v1/action.yml),
and its [MinIO client constructor](https://github.com/tespkg/actions-cache/blob/v1/src/utils.ts)
does not read that environment variable or configure path style. Thus
`--path-style off` changes stored/displayed configuration but not this
integration's addressing behavior. The sccache hand-off is a separate path.
Use a client with an explicit supported setting, or reject/document the
unsupported combination. String assertions against emitted YAML do not
constitute an integration test against the action/backend.

## Requirement coverage

“Fixture evidence” below means the relevant tests passed; it is deliberately
not a claim of live fleet acceptance. Numbered acceptance entries follow
each GitHub issue's order.

| Issue | Implementation and acceptance audit |
| --- | --- |
| [#52](https://github.com/joeblau/homebrew-hb/issues/52) | Harness, JSONL/raw logs, phase parsing, sampled CPU/RSS, host swap/disk deltas, unsupported lists, and comparison flags exist. Disk I/O rate is explicitly unsupported. **A1:** scenario commands/retained results exist, but the documented workload fails repetition (finding 11). **A2:** medians/p95 exist; independent repetition counting is wrong (10). **A3:** pinning/resource equality is operator instructions, not recorded/enforced verification; command/host warnings do not verify source, output, toolchain, or limits. **A4:** phase/queue fields exist, but overlap durations are wrong (9) and queue delay is supplied rather than measured by a fleet integration. **A5:** failed/cancelled records have fixtures; no-orphan requirement fails (4). **A6:** failure/swap/cache-growth flags have fixtures; no measured fleet speedup is established. Staging baseline absent. |
| [#53](https://github.com/joeblau/homebrew-hb/issues/53) | Per-runner `_toolcache`, plist environment, migration, upgrade/rollback carry-over, and offline pruning exist. Upstream confirms `AGENT_TOOLSDIRECTORY` support. **A1:** consecutive mocked cycles reuse tools; real setup-action jobs unverified. **A2:** default reset removes checkout/temp/registration and retains operator `.env`/`.path`, with fixture evidence. **A3:** defaults separate runner caches and prune requires offline acknowledgement; custom effective paths are not safely validated (7), and operator-shared caches have no concurrent-install guarantee. **A4:** lifecycle fixtures cover default persistence and repair/upgrade; effective override mismatch remains (7). **A5:** migration/rollback instructions exist; aliases and existing destination caches need care, since legacy data is deleted during reset when migration is skipped. **A6:** basic path/default isolation tests exist; physical-path, ownership, and upstream-precedence coverage is insufficient. |
| [#54](https://github.com/joeblau/homebrew-hb/issues/54) | Opt-in scoped SSD directories, ports, caps, configuration precedence, wrapper examples, permissions, stats JSON, and shared-backend/unsupported-case docs exist. **A1/A2:** real warm/source/compiler/target/flag builds were not demonstrated. **A3:** LRU is delegated to sccache, not exercised with active builds; manual uninstall is best-effort stop followed by deletion, with no idle gate. **A4:** advertised server fallback is incorrect (5). **A5:** scope names/ports have fixtures; real concurrent compiler behavior is unverified. **A6:** examples are emitted but not built; benchmark stats parser expects `key=value`, not the helper's JSON, and no automatic adapter is wired. Packaging blocked (1). |
| [#55](https://github.com/joeblau/homebrew-hb/issues/55) | Batch/budget flags, sparse-number planning, queue label filtering/pagination, exact local identity, maintenance locking, partial-failure logs, polling budget and primary/standby docs exist. **A1:** bounded batch/max/budget fixtures pass. **A2:** registered online idle lookup exists; partial-failure `actual` counts directories, and unregistered remnants make later `validate_fleet` abort rather than recover. In-flight registration is not measured as usable capacity. **A3:** minimum/gap/partial-failure/maintenance tests exist; runner-list API test codifies contrary behavior (8). **A4:** conservative exact-identity scale-down fixtures pass, with the existing observation-to-removal race documented. **A5:** no staging queue-latency/CPU/swap comparison. **A6:** process versus physical capacity and per-host bounds are documented; multi-host coordination remains operator-managed, not a shared fleet reservation. |
| [#56](https://github.com/joeblau/homebrew-hb/issues/56) | Opt-in setup/supervisor wiring, explicit budget, memory check, one bounded volume, per-job dirs, SSD fallback, environment propagation, sandbox allow-list, reboot handling and cleanup hooks exist. **A1:** distinct ordinary identities have fixtures, but ledger identities collide (2); per-job size is a reservation, not a filesystem quota. **A2:** disabled/unavailable/low-memory paths have fixtures. **A3:** cleanup/symlink fixtures exist but active-allocation invariant fails (2). **A4:** RAM happy-path copy-out exists; fallback and failed-copy retention fail (3). **A5:** mount/fallback/cancellation tests mock macOS commands; important collision/copy-failure cases are missing. **A6:** no same-workload SSD/RAM staging measurement. **A7:** remains opt-in. Packaging blocked (1). |
| [#57](https://github.com/joeblau/homebrew-hb/issues/57) | Configuration-only remote mode, endpoint/port/TLS/region, scoped buckets, credential files, local compatibility, protocol distinction and deployment/retention/writer docs exist. Path-style integration is incomplete (12). **A1:** no two-Mac reuse evidence. **A2:** example keys include repo/OS/arch/dependency inputs and docs require toolchain scoping; actual compiled/dependency entries were not inspected. **A3:** concurrent-writer/backend-integrity behavior is delegated to the action/backend, without live validation. **A4:** outage choices are documented but no real outage build was run; displayed behavior should be checked against the pinned action. **A5:** client tests prove no local service setup and preserve local-mode fixture behavior. **A6:** normal fixture credentials are absent from output/config and stored 0600, but arbitrary credential round trips fail (6). **A7:** no LAN restore/save/full-job comparison. |

## Verification and provenance

- `python3 -m unittest discover -s tests`: **385 total, 384 passed, 1 failed**
  in 84.678 seconds. The pasted statement “385 tests pass; single failure”
  overcounts the passing tests.
- Failure: `RunnerMcpTests.test_command_failure_and_missing_command_are_tool_errors`,
  `tests/test_runner_agent_tools.py:345`. Reproduced the same assertion failure
  from a temporary `git archive 42a851f^` extraction. `runner-mcp` and this test
  are unchanged by the six features. This confirms a pre-existing failure;
  it does not excuse the independent findings above.
- `bash -n` passed on all **19 Bash runner scripts** detected by their shebang.
  `runner-mcp` is Python, not a twentieth Bash script.
- `ruby -c Formula/runner-setup.rb`: syntax OK. `git diff --check`: clean.
  A complete Homebrew archive install/test was not run; source-content
  inspection already proves the missing-file packaging defect.
- All six cited feature commits are ancestors of local `main`; all six
  `issue-*` branches are merged. `git worktree list` shows only the main
  checkout. These facts support the merge/cleanup claim but cannot prove
  historical absence of worktree collisions.
- README and formula install/test lists name the three new tools.
- All six issues were OPEN when queried with `gh issue view` during this audit.
- Additional reproductions used temporary directories, existing fixture
  functions, dummy credentials, and short local benchmark processes. No
  runner fleet, production cache, live RAM disk, service, or remote issue was
  modified. The benchmark orphan created by the audit was explicitly killed.

## Remaining validation before claiming implementation completion

Fix the confirmed defects and add regression coverage for the actual failure
conditions. Then exercise the published formula against its source archive.
Run a pinned, repeatable benchmark with five independent repetitions per
configuration and retain raw results. Staging validation is needed for all
six issues: baseline/cancellation (#52), real two-job tool reuse (#53),
compiler hits and invalidation/fallback (#54), burst queue/full-job latency
(#55), real mount/cancellation and SSD-vs-RAM memory/swap behavior (#56), and
two-Mac remote reuse/concurrent writers/outage/transfer cost (#57). The pasted
“not done” list omitted the explicit #54 staging builds and the baseline
validation for #52. No staging success or performance gain is claimed here.
