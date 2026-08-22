"""Regenerate docs/parallelism-data/runs/<run_id>.json: an exact step-function
trace of how many Galaxy jobs were actually concurrent throughout each run,
split by destination (GCP Batch vs. the node's own local k8s runner).

Built from the job.create_time/update_time and job.external_id already
present in the raw reports/anvil/tool-tests/<run_id>/results.json (the same
file anvil_generate_raster_data.py reads) - no new capture during the run,
and works retroactively on every run already committed. This is an
approximation of concurrency (a job's [create_time, update_time] interval
includes any time it spent queued, not just actually executing), not a
direct measurement - see v2_brief.md-adjacent discussion for why a live
poller would be a more precise follow-up.

Breakpoints are emitted at every job start/end (not sampled at a fixed
interval), so the trace is exact and a brief dip or spike is never
smoothed away - a chart can render each category's breakpoint list directly
as a step line.

Usage: anvil_generate_parallelism_data.py
"""

import json
import os
from datetime import datetime

TOOL_TESTS_DIR = "reports/anvil/tool-tests"
PARALLELISM_DATA_DIR = "docs/parallelism-data"
RUNS_DIR = f"{PARALLELISM_DATA_DIR}/runs"

CATEGORIES = ("gcp_batch", "local_k8s", "other")


def all_run_dirs() -> list[str]:
    if not os.path.isdir(TOOL_TESTS_DIR):
        return []
    names = sorted(os.listdir(TOOL_TESTS_DIR))
    return [
        n
        for n in names
        if os.path.isdir(os.path.join(TOOL_TESTS_DIR, n))
        and os.path.exists(os.path.join(TOOL_TESTS_DIR, n, "results.json"))
    ]


def load_tests(run_id: str) -> list[dict]:
    path = os.path.join(TOOL_TESTS_DIR, run_id, "results.json")
    with open(path) as f:
        data = json.load(f)
    return data.get("tests", [])


def classify_destination(external_id: str) -> str:
    if external_id.startswith("galaxy-batch-"):
        return "gcp_batch"
    if external_id.startswith("gxy-"):
        return "local_k8s"
    return "other"


def collect_intervals(tests: list[dict]) -> dict[str, list[tuple]]:
    """destination -> [(start, end), ...] job intervals for one run."""
    intervals: dict[str, list[tuple]] = {c: [] for c in CATEGORIES}
    for t in tests:
        job = t.get("data", {}).get("job")
        if not job:
            continue
        ct, ut = job.get("create_time"), job.get("update_time")
        if not ct or not ut:
            continue
        start, end = datetime.fromisoformat(ct), datetime.fromisoformat(ut)
        if end < start:
            continue
        dest = classify_destination(job.get("external_id") or "")
        intervals[dest].append((start, end))
    return intervals


def build_step_function(intervals: list[tuple]) -> list[list]:
    """Exact breakpoints [[iso_timestamp, concurrent_count], ...] of the
    concurrency step function - one entry per job start/end, in
    chronological order. Simultaneous events are ordered start-before-end
    so two jobs that touch at the same instant count as briefly
    overlapping, rather than an artificial dip to a lower count."""
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda ev: (ev[0], -ev[1]))

    breakpoints = []
    level = 0
    for t, delta in events:
        level += delta
        breakpoints.append([t.isoformat(), level])
    return breakpoints


def main() -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    written = 0
    for run_id in all_run_dirs():
        tests = load_tests(run_id)
        intervals_by_dest = collect_intervals(tests)
        all_intervals = [iv for ivs in intervals_by_dest.values() for iv in ivs]
        if not all_intervals:
            continue

        series = {dest: build_step_function(ivs) for dest, ivs in intervals_by_dest.items()}
        series["total"] = build_step_function(all_intervals)

        peak = {dest: max((level for _, level in bps), default=0) for dest, bps in series.items()}
        run_start = min(start for start, _ in all_intervals)
        run_end = max(end for _, end in all_intervals)

        out_path = os.path.join(RUNS_DIR, f"{run_id}.json")
        with open(out_path, "w") as f:
            json.dump(
                {
                    "run_id": run_id,
                    "start": run_start.isoformat(),
                    "end": run_end.isoformat(),
                    "jobs_with_timing": len(all_intervals),
                    "peak": peak,
                    "series": series,
                },
                f,
                separators=(",", ":"),
            )
        written += 1

    print(f"Wrote {written} run parallelism traces under {RUNS_DIR}/")


if __name__ == "__main__":
    main()
