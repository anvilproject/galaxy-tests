#!/usr/bin/env python3
"""Summarize tool-matrix runs at several concurrency levels into a scaling table.

Usage: anvil_matrix_scaling.py MATRIX_ROOT OUTPUT_JSON

MATRIX_ROOT holds one directory per level as written by
anvil_tool_matrix.py run --output-dir MATRIX_ROOT/x<K> (one JSON per job plus
matrix-run.json). For every level this reports the replica count, jobs by
state, wall-clock from the first job's creation to the last job's end, total
and median container runtime, median and 95th percentile queue wait (job
creation to start_epoch, which is when the job began on its VM or pod), the
job throughput, and per-tool median runtime so runtime inflation under
contention is visible. It prints a table and writes the same data as JSON.
Stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone


def parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def metric(job_metrics, name):
    for entry in job_metrics or []:
        if entry.get("name") == name:
            try:
                return float(entry.get("raw_value", entry.get("value")))
            except (TypeError, ValueError):
                return None
    return None


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def tool_short(tool_id):
    parts = (tool_id or "").split("/")
    return parts[-2] if len(parts) >= 2 else tool_id


def summarize_level(path):
    run_info = {}
    run_file = os.path.join(path, "matrix-run.json")
    if os.path.exists(run_file):
        run_info = json.load(open(run_file))
    jobs = []
    for f in glob.glob(os.path.join(path, "*.json")):
        if f.endswith("matrix-run.json"):
            continue
        try:
            rec = json.load(open(f))
        except ValueError:
            continue
        if "metrics" in rec:
            jobs.append(rec)
    states = defaultdict(int)
    runners = defaultdict(int)
    creates, ends, runtimes, waits = [], [], [], []
    per_tool = defaultdict(list)
    for rec in jobs:
        job = rec["metrics"]
        states[job.get("state", "?")] += 1
        runners[(job.get("job_runner_name") or "?").split(":")[0]] += 1
        jm = job.get("job_metrics") or []
        created = parse_ts(job.get("create_time"))
        start = metric(jm, "start_epoch")
        end = metric(jm, "end_epoch")
        rt = metric(jm, "runtime_seconds")
        if created:
            creates.append(created.timestamp())
        if end:
            ends.append(end)
        if rt is not None:
            runtimes.append(rt)
            per_tool[rec.get("tool") or tool_short(job.get("tool_id"))].append(rt)
        if created and start:
            waits.append(max(0.0, start - created.timestamp()))
    wall = (max(ends) - min(creates)) if creates and ends else None
    return {
        "level": os.path.basename(path.rstrip("/")),
        "replicas": run_info.get("replicas"),
        "entries": len(run_info.get("entries") or []),
        "jobs": len(jobs),
        "states": dict(states),
        "runners": dict(runners),
        "entry_states": dict(defaultdict(int, {s: list(run_info.get("states", {}).values()).count(s)
                                                for s in set(run_info.get("states", {}).values())})),
        "submit_errors": len([e for e in run_info.get("errors", []) if e.get("phase") == "submit"]),
        "wall_seconds": round(wall, 1) if wall is not None else None,
        "driver_elapsed_seconds": run_info.get("elapsed_seconds"),
        "runtime_total_seconds": round(sum(runtimes), 1),
        "runtime_median_seconds": round(statistics.median(runtimes), 1) if runtimes else None,
        "runtime_max_seconds": round(max(runtimes), 1) if runtimes else None,
        "queue_wait_median_seconds": round(pct(waits, 0.5), 1) if waits else None,
        "queue_wait_p95_seconds": round(pct(waits, 0.95), 1) if waits else None,
        "queue_wait_max_seconds": round(max(waits), 1) if waits else None,
        "jobs_per_hour": round(len(jobs) / (wall / 3600), 1) if wall else None,
        "tool_median_runtime": {t: round(statistics.median(v), 1) for t, v in sorted(per_tool.items())},
    }


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    root, out = sys.argv[1], sys.argv[2]
    levels = []
    for path in sorted(glob.glob(os.path.join(root, "x*")),
                       key=lambda p: int(os.path.basename(p)[1:]) if os.path.basename(p)[1:].isdigit() else 0):
        if os.path.isdir(path):
            levels.append(summarize_level(path))
    if not levels:
        print(f"No level directories under {root}", file=sys.stderr)
        json.dump({"levels": []}, open(out, "w"), indent=2)
        return 0
    cols = [("level", "level"), ("replicas", "repl"), ("jobs", "jobs"), ("wall_seconds", "wall s"),
            ("runtime_total_seconds", "runtime s"), ("runtime_median_seconds", "rt med"),
            ("queue_wait_median_seconds", "wait med"), ("queue_wait_p95_seconds", "wait p95"),
            ("jobs_per_hour", "jobs/h")]
    print("  ".join(f"{h:>10}" for _, h in cols) + "  states")
    for lv in levels:
        row = "  ".join(f"{str(lv.get(k) if lv.get(k) is not None else '-'):>10}" for k, _ in cols)
        print(row + "  " + ", ".join(f"{k}={v}" for k, v in sorted(lv["states"].items())))
    # per-tool inflation: median runtime relative to the first level
    base = levels[0]["tool_median_runtime"]
    inflation = {}
    for lv in levels[1:]:
        for t, v in lv["tool_median_runtime"].items():
            if t in base and base[t] >= 30:  # only tools long enough to measure
                inflation.setdefault(t, {})[lv["level"]] = round(v / base[t], 2) if base[t] else None
    if inflation:
        worst = sorted(inflation.items(), key=lambda kv: -max(x for x in kv[1].values() if x), )[:10]
        print("\nRuntime inflation vs first level (tools with median >= 30 s):")
        for t, levels_ratio in worst:
            print(f"  {t:<32} " + "  ".join(f"{l}={r}x" for l, r in levels_ratio.items()))
    json.dump({"levels": levels, "runtime_inflation": inflation}, open(out, "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
