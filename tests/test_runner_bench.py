"""runner-bench: scenario orchestration, timing/result parsing, reports,
and failed/cancelled runs.

All fixtures live in a temp directory; ps/sysctl/df are mocked with shell
functions and the real /opt, sudo, launchd, and production caches are never
touched. The script is sourced so globals can be redirected at the fixtures
(the BASH_SOURCE guard keeps main from running on source).
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "runner-bench"

PRELUDE = r'''
source "$1"
trap - EXIT
TEST_ROOT="$2"
HOME="${TEST_ROOT}/home"
BENCH_ROOT="${TEST_ROOT}/bench"
WORKDIR="${TEST_ROOT}/work"
CACHE_DIR=""
RESULTS_DIR="${TEST_ROOT}/results"
HOST_LABEL=test-host
NOW=1000
detect_platform() { :; }
detect_host() { HOST_LABEL=test-host; }
now_epoch() { NOW=$((NOW+30)); NOW_EPOCH="${NOW}"; }
ps() { printf ' 10.0 102400\n 20.0 204800\n'; }
sysctl() {
  if [[ "${1:-}" == "vm.swapusage" ]]; then
    printf 'vm.swapusage: total = 2048.00M  used = 512.00M  free = 1536.00M  (encrypted)\n'
    return 0
  fi
  case "${2:-}" in
    hw.model)   printf 'Mac14,3\n' ;;
    hw.ncpu)    printf '12\n' ;;
    hw.memsize) printf '34359738368\n' ;;
    *)          printf '\n' ;;
  esac
}
df() { printf 'Filesystem 1024-blocks Used Available Capacity\n/dev/disk1s1 1000000 500000 500000 50%%\n'; }
sw_vers() { printf '15.6\n'; }
mkdir -p "${HOME}" "${WORKDIR}" "${RESULTS_DIR}"
'''

# Workload fixture: phase markers at fixed epochs (start is 1030 after the
# mocked now_epoch sequence), plus cache counters in the stats file.
WORKLOAD = (
    'printf "RUNNER_BENCH_PHASE setup 1040\\nRUNNER_BENCH_PHASE build 1050\\n"; '
    'printf "cache_hits=12\\ncache_misses=3\\ncache_restore_s=2\\ncache_save_s=1\\n" '
    '> "${RUNNER_BENCH_STATS_FILE}"'
)

# Fixed per-job sample values; the real tree walk is covered by its own test.
TREE_MOCK = "tree_stats() { printf '25.0 384'; }\n"


def workload_body(extra_args="", reps=1):
    """Shell body that runs one WORKLOAD scenario (quoted-heredoc safe)."""
    return (
        "WL=$(cat <<'EOF'\n" + WORKLOAD + "\nEOF\n)\n"
        + TREE_MOCK
        + 'main run --reps %d --interval 1 --workdir "${WORKDIR}" '
          '--results-dir "${RESULTS_DIR}" %s --cmd "${WL}"' % (reps, extra_args)
    )

HOST = {"label": "test-host", "model": "Mac14,3", "cpu_count": 12,
        "mem_bytes": 34359738368, "arch": "arm64", "macos": "15.6"}


def job_record(label, scenario, concurrency, rep, duration, result="success",
               queue_delay=None, phases=None, rss_peak=400, cache_growth=100,
               host=HOST):
    return {
        "schema_version": 1, "ts": "2026-09-21T10:00:00Z", "run_id": "x",
        "label": label, "toolchain": "fixture-toolchain", "scenario": scenario,
        "concurrency": concurrency, "repetition": rep, "command": "build.sh",
        "host": host, "queued_at": None,
        "started_at": "2026-09-21T10:00:00Z", "finished_at": "2026-09-21T10:01:00Z",
        "queue_delay_s": queue_delay, "duration_s": duration,
        "scope": "job", "slot": 0, "workdir": "/w", "exit_code": 0,
        "result": result, "phases": phases or {},
        "cache": {"hits": None, "misses": None, "restore_s": None,
                  "save_s": None, "growth_kb": cache_growth, "stats": {}},
        "resources": {"samples": 3, "cpu_mean_pct": 50.0,
                      "cpu_peak_pct": 90.0, "rss_peak_mb": rss_peak},
        "unsupported": [], "artifacts": {},
    }


def host_record(label, scenario, concurrency, rep, swap_growth):
    rec = job_record(label, scenario, concurrency, rep, 60)
    rec.update({"scope": "host", "result": "success",
                "resources": {"samples": 3, "cpu_mean_pct": 30.0,
                              "cpu_peak_pct": 80.0, "rss_peak_mb": 2000,
                              "swap_used_mb_peak": 512.0,
                              "swap_growth_mb": swap_growth,
                              "disk_used_delta_kb": 64}})
    return rec


class RunnerBenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-bench-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def shell(self, body, timeout=60):
        return subprocess.run(
            ["/bin/bash", "-c", PRELUDE + "\n" + body, "test", str(SCRIPT), str(self.root)],
            text=True, capture_output=True, timeout=timeout)

    def results(self):
        path = self.root / "results/runner-bench.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def write_results(self, records):
        path = self.root / "results/runner-bench.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            for rec in records:
                handle.write(json.dumps(rec) + "\n")
        return path

    # -- argument handling ---------------------------------------------------

    def test_help(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("USAGE:", result.stderr)
        for word in ("run", "report", "cold", "warm", "edited", "--baseline"):
            self.assertIn(word, result.stderr)

    def test_missing_mode_exits_2(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("USAGE:", result.stderr)

    def test_unknown_argument_exits_2(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT), "run", "--bogus"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("USAGE:", result.stderr)

    def test_run_requires_cmd(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT), "run"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_bad_scenario_exits_2(self):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "run", "--cmd", "true", "--scenario", "bogus"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_baseline_without_candidate_exits_2(self):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "report", "--baseline", "a"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    # -- run mode: timing/result parsing --------------------------------------

    def test_run_records_phases_stats_resources_and_unsupported(self):
        result = self.shell(workload_body('--label t1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.results()
        self.assertEqual(len(records), 2)

        job = next(r for r in records if r["scope"] == "job")
        self.assertEqual(job["result"], "success")
        self.assertEqual(job["exit_code"], 0)
        self.assertGreaterEqual(job["duration_s"], 0)
        self.assertEqual(set(job["phases"]), {"setup", "build"})
        self.assertEqual(job["phases"]["build"], 10)
        self.assertEqual(job["label"], "t1")
        self.assertEqual(job["scenario"], "warm")
        self.assertEqual(job["cache"]["hits"], 12)
        self.assertEqual(job["cache"]["misses"], 3)
        self.assertEqual(job["cache"]["restore_s"], 2)
        self.assertEqual(job["cache"]["save_s"], 1)
        self.assertEqual(job["cache"]["growth_kb"], 0)
        self.assertEqual(job["resources"]["cpu_peak_pct"], 25.0)
        self.assertEqual(job["resources"]["rss_peak_mb"], 384)
        self.assertGreaterEqual(job["resources"]["samples"], 2)
        self.assertEqual(job["host"]["model"], "Mac14,3")
        self.assertEqual(job["host"]["cpu_count"], 12)
        # No --queued-at: queue delay is null and flagged unsupported.
        self.assertIsNone(job["queue_delay_s"])
        self.assertIn("queue_delay_s", job["unsupported"])
        self.assertIn("disk_io_rate", job["unsupported"])

        host = next(r for r in records if r["scope"] == "host")
        self.assertEqual(host["result"], "success")
        self.assertEqual(host["resources"]["cpu_mean_pct"], 30.0)
        self.assertEqual(host["resources"]["rss_peak_mb"], 300)
        self.assertEqual(host["resources"]["swap_growth_mb"], 0.0)

        # Raw artifacts retained: samples and the workload log.
        self.assertTrue(Path(job["artifacts"]["samples"]).exists())
        self.assertTrue(Path(job["artifacts"]["log"]).exists())
        self.assertIn("RUNNER_BENCH_PHASE", Path(job["artifacts"]["log"]).read_text())

    def test_run_records_queue_delay_when_queued_at_given(self):
        result = self.shell(workload_body('--label t1 --queued-at 990'))
        self.assertEqual(result.returncode, 0, result.stderr)
        job = next(r for r in self.results() if r["scope"] == "job")
        from datetime import datetime
        start = datetime.fromisoformat(job["started_at"]).timestamp()
        self.assertAlmostEqual(job["queue_delay_s"], start - 990, places=5)
        self.assertEqual(job["queue_timing_source"], "batch_supplied")
        self.assertNotIn("queue_delay_s", job["unsupported"])

    def test_run_without_stats_file_marks_cache_counters_unsupported(self):
        result = self.shell(workload_body('--label t1').replace('${WL}', 'true'))
        self.assertEqual(result.returncode, 0, result.stderr)
        job = next(r for r in self.results() if r["scope"] == "job")
        self.assertIsNone(job["cache"]["hits"])
        self.assertIn("cache_counters", job["unsupported"])

    # -- run mode: scenarios --------------------------------------------------

    def test_cold_wipes_only_the_disposable_bench_cache(self):
        cache = self.root / "work/.bench-cache"
        cache.mkdir(parents=True)
        (cache / "marker.txt").write_text("cold-me")
        keep = self.root / "work/keep.txt"
        keep.write_text("keep-me")
        result = self.shell(workload_body('--scenario cold --label t1').replace('${WL}', 'true'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((cache / "marker.txt").exists())
        self.assertTrue(keep.exists())

    def test_cold_refuses_cache_dir_outside_workdir(self):
        outside = self.root / "production-cache"
        outside.mkdir()
        (outside / "prod.txt").write_text("do-not-touch")
        result = self.shell(
            f'main run --scenario cold --reps 1 --workdir "${{WORKDIR}}" '
            f'--cache-dir "{outside}" --results-dir "${{RESULTS_DIR}}" --cmd true')
        self.assertEqual(result.returncode, 2)
        self.assertIn("inside --workdir", result.stderr)
        self.assertTrue((outside / "prod.txt").exists())

    def test_edited_appends_small_edit_per_rep(self):
        result = self.shell(
            f'main run --scenario edited --reps 2 --interval 1 '
            f'--workdir "${{WORKDIR}}" --results-dir "${{RESULTS_DIR}}" --cmd true')
        self.assertEqual(result.returncode, 0, result.stderr)
        edit = self.root / "work/BENCH_EDIT.txt"
        lines = edit.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("rep=1", lines[0])
        self.assertIn("rep=2", lines[1])

    def test_concurrency_records_per_slot_jobs_and_one_host(self):
        result = self.shell(
            f'main run --reps 1 --concurrency 2 --interval 1 '
            f'--workdir "${{WORKDIR}}" --results-dir "${{RESULTS_DIR}}" --cmd true')
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.results()
        jobs = [r for r in records if r["scope"] == "job"]
        hosts = [r for r in records if r["scope"] == "host"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(hosts), 1)
        self.assertEqual(sorted(r["slot"] for r in jobs), [0, 1])
        self.assertEqual({r["concurrency"] for r in records}, {2})
        self.assertTrue((self.root / "work/slot-0").is_dir())
        self.assertTrue((self.root / "work/slot-1").is_dir())

    # -- run mode: failure and cancellation -----------------------------------

    def test_failed_workload_is_recorded_and_run_continues(self):
        result = self.shell(
            f'main run --reps 2 --interval 1 --workdir "${{WORKDIR}}" '
            f'--results-dir "${{RESULTS_DIR}}" --cmd \'exit 3\'')
        self.assertEqual(result.returncode, 1)
        jobs = [r for r in self.results() if r["scope"] == "job"]
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertEqual(job["result"], "failed")
            self.assertEqual(job["exit_code"], 3)

    def test_exit_cleans_reparented_children_on_success_and_failure(self):
        for code in (0, 1):
            with self.subTest(code=code):
                body = r'''
ps() { command ps "$@"; }
main run --reps 1 --interval 1 --workdir "${WORKDIR}" --results-dir "${RESULTS_DIR}" \
  --cmd 'sleep 60 & echo $! > child.pid; exit %d'
''' % code
                result = self.shell(body)
                self.assertEqual(result.returncode, code, result.stderr)
                pid = (self.root / "work/child.pid").read_text().strip()
                probe = subprocess.run(["ps", "-p", pid, "-o", "stat="], text=True, capture_output=True)
                self.assertTrue(probe.returncode != 0 or probe.stdout.strip().startswith("Z"), probe.stdout)

    def test_each_slot_records_its_own_duration(self):
        result = self.shell("""main run --reps 1 --interval 1 --concurrency 2 --workdir "${WORKDIR}" --results-dir "${RESULTS_DIR}" --cmd 'if [ "$RUNNER_BENCH_SLOT" = 0 ]; then sleep 2; else sleep 0.2; fi'""")
        self.assertEqual(result.returncode, 0, result.stderr)
        jobs = {r["slot"]: r for r in self.results() if r["scope"] == "job"}
        self.assertGreater(jobs[0]["duration_s"] - jobs[1]["duration_s"], 1)
        self.assertNotEqual(jobs[0]["finished_at"], jobs[1]["finished_at"])

    def test_concurrent_jobs_do_not_satisfy_repetition_gate(self):
        records = []
        for label in ("baseline", "candidate"):
            for slot in range(5):
                rec = job_record(label, "warm", 5, 1, 2)
                rec["slot"] = slot
                records.append(rec)
            records.append(host_record(label, "warm", 5, 1, 0))
        self.write_results(records)
        result = self.shell('main report --results-dir "${RESULTS_DIR}" --baseline baseline --candidate candidate --json')
        data = json.loads(result.stdout)
        self.assertEqual(data["groups"][0]["runs"], 1)
        self.assertEqual(data["groups"][0]["job_samples"], 5)
        self.assertTrue(any("fewer than 5" in w for w in data["comparisons"][0]["warnings"]))

    def test_documented_workload_runs_five_repetitions_in_all_scenarios(self):
        doc = (SCRIPT.parent / "docs/runner-performance.md").read_text()
        workload = doc.split("BENCH=$(cat <<'WORKLOAD'\n", 1)[1].split("\nWORKLOAD\n", 1)[0]
        for scenario in ("cold", "warm", "edited"):
            body = "WL=$(cat <<'DOCWORKLOAD'\n" + workload + "\nDOCWORKLOAD\n)\n"
            body += f'main run --reps 5 --interval 1 --scenario {scenario} --workdir "${{WORKDIR}}" --results-dir "${{RESULTS_DIR}}" --cmd "$WL"'
            result = self.shell(body)
            self.assertEqual(result.returncode, 0, result.stderr)
        jobs = [r for r in self.results() if r["scope"] == "job"]
        self.assertEqual(len(jobs), 15)
        self.assertTrue(all(r["result"] == "success" for r in jobs))
        self.assertIn('value=5', (self.root / "work/src/main.c").read_text())

    def test_cancelled_run_records_result_and_leaves_no_orphans(self):
        marker = self.root / "child.pid"
        body = r'''
main run --reps 3 --interval 1 --workdir "${WORKDIR}" \
  --results-dir "${RESULTS_DIR}" \
  --cmd 'sleep 30 & echo $! > "%s"; wait' &
bench=$!
sleep 2
kill -TERM "${bench}"
rc=0
wait "${bench}" || rc=$?
echo "rc=${rc}"
kid="$(cat "%s")"
if kill -0 "${kid}" 2>/dev/null; then echo "child-alive"; else echo "child-reaped"; fi
''' % (marker, marker)
        # kill_tree and the sampler need real ps here, not the fixture mock.
        # (SIGTERM because non-interactive background shells start with
        # SIGINT ignored, and ignored-on-entry signals cannot be trapped.)
        prelude = PRELUDE.replace("ps() { printf ' 10.0 102400\\n 20.0 204800\\n'; }",
                                  'ps() { command ps "$@"; }')
        result = subprocess.run(
            ["/bin/bash", "-c", prelude + "\n" + body, "test", str(SCRIPT), str(self.root)],
            text=True, capture_output=True, timeout=60)
        self.assertIn("rc=130", result.stdout)
        self.assertIn("child-reaped", result.stdout)
        jobs = [r for r in self.results() if r["scope"] == "job"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["result"], "cancelled")
        hosts = [r for r in self.results() if r["scope"] == "host"]
        self.assertEqual(hosts[0]["result"], "cancelled")

    # -- sampling primitives ---------------------------------------------------

    def test_tree_stats_sums_process_tree(self):
        body = r'''
ps() {
  printf '%s\n' \
    '  100    1  5.0   1024 init' \
    '  200  100 10.0 204800 worker' \
    '  300  200  1.0   1024 grandchild' \
    '  400    1 99.0 999999 unrelated'
}
tree_stats 100
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "16.0 202")

    def test_kill_tree_reaps_descendants(self):
        marker = self.root / "kid.pid"
        body = r'''
ps() { command ps "$@"; }
( sleep 30 & echo $! > "%s"; wait ) &
root=$!
sleep 1
kill_tree "${root}"
rc_root=0; kill -0 "${root}" 2>/dev/null || rc_root=1
kid="$(cat "%s")"
rc_kid=0; kill -0 "${kid}" 2>/dev/null || rc_kid=1
echo "root_gone=${rc_root} kid_gone=${rc_kid}"
''' % (marker, marker)
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("root_gone=1 kid_gone=1", result.stdout)

    # -- report mode -----------------------------------------------------------

    def test_report_median_p95_sample_size_and_regressions(self):
        records = []
        for i, dur in enumerate((30, 32, 34, 36, 38)):
            records.append(job_record("baseline", "warm", 1, i + 1, dur,
                                      queue_delay=10, phases={"build": dur - 5},
                                      cache_growth=100))
            records.append(host_record("baseline", "warm", 1, i + 1, 0.0))
        for i, dur in enumerate((20, 21, 22, 23, 24)):
            result = "failed" if i == 4 else "success"
            records.append(job_record("candidate", "warm", 1, i + 1, dur,
                                      queue_delay=10, result=result,
                                      cache_growth=150))
            records.append(host_record("candidate", "warm", 1, i + 1, 25.0))
        self.write_results(records)

        result = self.shell(
            'main report --results-dir "${RESULTS_DIR}" '
            '--baseline baseline --candidate candidate')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("label=baseline scenario=warm concurrency=1", out)
        self.assertIn("n=5", out)
        self.assertIn("duration_s: median 34  p95 38", out)
        self.assertIn("queue_delay_s: median 10", out)
        self.assertIn("build", out)
        self.assertIn("compare baseline 'baseline' -> candidate 'candidate'", out)
        self.assertIn("duration_s", out)
        self.assertIn("-35.3%", out)  # 34 -> 22 median
        self.assertIn("REGRESSION: failures increased 0 -> 1", out)
        self.assertIn("REGRESSION: swap growth median increased 0 -> 25", out)
        self.assertIn("REGRESSION: cache growth median increased 100 -> 150", out)
        self.assertIn("no speedup is", out)

    def test_report_json_is_machine_readable(self):
        records = []
        for i in range(5):
            records.append(job_record("baseline", "cold", 1, i + 1, 40 + i))
            records.append(host_record("baseline", "cold", 1, i + 1, 1.0))
        self.write_results(records)
        result = self.shell('main report --results-dir "${RESULTS_DIR}" --json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len(data["groups"]), 1)
        group = data["groups"][0]
        self.assertEqual(group["runs"], 5)
        self.assertEqual(group["duration_s"]["median"], 42.0)
        self.assertEqual(group["duration_s"]["p95"], 44.0)
        self.assertEqual(group["host"]["model"], "Mac14,3")
        self.assertEqual(group["swap_growth_mb"]["median"], 1.0)

    def test_report_warns_below_five_repetitions(self):
        records = []
        for i in range(2):
            records.append(job_record("baseline", "warm", 1, i + 1, 30))
        self.write_results(records)
        result = self.shell('main report --results-dir "${RESULTS_DIR}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: fewer than 5 repetitions", result.stdout)

    def test_report_distinguishes_scenarios_and_concurrency(self):
        records = [
            job_record("baseline", "warm", 1, 1, 30),
            job_record("baseline", "cold", 1, 1, 50),
            job_record("baseline", "warm", 2, 1, 45),
        ]
        self.write_results(records)
        result = self.shell('main report --results-dir "${RESULTS_DIR}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scenario=warm concurrency=1", result.stdout)
        self.assertIn("scenario=cold concurrency=1", result.stdout)
        self.assertIn("scenario=warm concurrency=2", result.stdout)

    def test_report_unavailable_metrics_are_named(self):
        self.write_results([job_record("baseline", "warm", 1, 1, 30)])
        result = self.shell('main report --results-dir "${RESULTS_DIR}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("queue_delay_s: unavailable", result.stdout)
        self.assertIn("swap_growth_mb: unavailable", result.stdout)

    def test_report_comparsion_warns_on_different_hosts(self):
        other = dict(HOST, model="Mac17,1")
        records = [job_record("baseline", "warm", 1, i + 1, 30) for i in range(5)]
        records += [job_record("candidate", "warm", 1, i + 1, 25, host=other)
                    for i in range(5)]
        self.write_results(records)
        result = self.shell(
            'main report --results-dir "${RESULTS_DIR}" '
            '--baseline baseline --candidate candidate')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: host hardware/OS differs", result.stdout)

    def test_report_without_records_fails(self):
        result = self.shell('main report --results-dir "${RESULTS_DIR}"')
        self.assertEqual(result.returncode, 1)
        self.assertIn("no benchmark records", result.stderr)


if __name__ == "__main__":
    unittest.main()
