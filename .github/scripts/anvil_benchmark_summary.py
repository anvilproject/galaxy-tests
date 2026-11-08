#!/usr/bin/env python3
"""Summarize the per-job metrics abm wrote for a benchmark run.

Usage: anvil_benchmark_summary.py METRICS_DIR OUTPUT_JSON

METRICS_DIR holds one JSON file per Galaxy job, as written by
`abm <cloud> benchmark run ... -e <experiment>` (metrics/<experiment>/*.json).
Each file carries the abm run context (run, cloud, history_id, inputs) and the
job's API record under "metrics", including job_metrics.

The summary groups jobs by history (one history per workflow invocation) and
reports, per history and overall: job counts by state and runner, wall-clock
from the first job's creation to the last job's completion, the sum of
container runtimes (the runtime_seconds job metric), and per-tool runtime.
It prints a table to stdout and writes the same data as JSON. Stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def tool_name(tool_id: str) -> str:
    parts = tool_id.split("/")
    return parts[-2] if len(parts) >= 2 else tool_id


def metric(job_metrics: list[dict], name: str) -> float | None:
    for entry in job_metrics or []:
        if entry.get("name") == name:
            raw = entry.get("raw_value", entry.get("value"))
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    metrics_dir, output_path = sys.argv[1], sys.argv[2]
    files = sorted(glob.glob(os.path.join(metrics_dir, "*.json")))
    if not files:
        print(f"No metrics files under {metrics_dir}", file=sys.stderr)
        json.dump({"histories": [], "totals": {"jobs": 0}}, open(output_path, "w"), indent=2)
        return 0

    histories: dict[str, dict] = defaultdict(lambda: {
        "jobs": 0, "states": defaultdict(int), "runners": defaultdict(int),
        "first_create": None, "last_update": None, "runtime_seconds": 0.0,
        "tools": defaultdict(lambda: {"jobs": 0, "runtime_seconds": 0.0, "max_runtime_seconds": 0.0, "states": defaultdict(int)}),
        "run": None, "inputs": None,
    })
    for path in files:
        try:
            record = json.load(open(path))
        except (OSError, ValueError) as exc:
            print(f"Skipping {path}: {exc}", file=sys.stderr)
            continue
        job = record.get("metrics") or {}
        hist = histories[record.get("history_id") or job.get("history_id") or "?"]
        hist["run"] = record.get("run")
        hist["inputs"] = record.get("inputs")
        hist["jobs"] += 1
        hist["states"][job.get("state") or "?"] += 1
        hist["runners"][job.get("job_runner_name") or "?"] += 1
        created, updated = parse_ts(job.get("create_time")), parse_ts(job.get("update_time"))
        if created and (hist["first_create"] is None or created < hist["first_create"]):
            hist["first_create"] = created
        if updated and (hist["last_update"] is None or updated > hist["last_update"]):
            hist["last_update"] = updated
        runtime = metric(job.get("job_metrics"), "runtime_seconds") or 0.0
        hist["runtime_seconds"] += runtime
        tool = hist["tools"][tool_name(job.get("tool_id") or "?")]
        tool["jobs"] += 1
        tool["runtime_seconds"] += runtime
        tool["max_runtime_seconds"] = max(tool["max_runtime_seconds"], runtime)
        tool["states"][job.get("state") or "?"] += 1

    out_histories = []
    totals = {"jobs": 0, "runtime_seconds": 0.0, "states": defaultdict(int), "runners": defaultdict(int)}
    print(f"{'history':18} {'run':>3} {'jobs':>5} {'ok':>4} {'err':>4} {'wall min':>9} {'tool h':>7}  inputs")
    for hid, h in sorted(histories.items(), key=lambda kv: kv[1]["first_create"] or datetime.max):
        wall = (h["last_update"] - h["first_create"]).total_seconds() if h["first_create"] and h["last_update"] else None
        entry = {
            "history_id": hid,
            "run": h["run"],
            "inputs": h["inputs"],
            "jobs": h["jobs"],
            "states": dict(h["states"]),
            "runners": dict(h["runners"]),
            "first_create": h["first_create"].isoformat() if h["first_create"] else None,
            "last_update": h["last_update"].isoformat() if h["last_update"] else None,
            "wall_seconds": round(wall, 1) if wall is not None else None,
            "runtime_seconds": round(h["runtime_seconds"], 1),
            "tools": {
                name: {
                    "jobs": t["jobs"],
                    "runtime_seconds": round(t["runtime_seconds"], 1),
                    "max_runtime_seconds": round(t["max_runtime_seconds"], 1),
                    "states": dict(t["states"]),
                }
                for name, t in sorted(h["tools"].items(), key=lambda kv: -kv[1]["runtime_seconds"])
            },
        }
        out_histories.append(entry)
        totals["jobs"] += h["jobs"]
        totals["runtime_seconds"] += h["runtime_seconds"]
        for k, v in h["states"].items():
            totals["states"][k] += v
        for k, v in h["runners"].items():
            totals["runners"][k] += v
        print(f"{hid:18} {str(h['run'] or ''):>3} {h['jobs']:5d} {h['states'].get('ok', 0):4d} "
              f"{h['states'].get('error', 0):4d} {(wall or 0) / 60:9.1f} {h['runtime_seconds'] / 3600:7.2f}  {h['inputs'] or ''}")

    summary = {
        "metrics_dir": metrics_dir,
        "histories": out_histories,
        "totals": {
            "jobs": totals["jobs"],
            "runtime_seconds": round(totals["runtime_seconds"], 1),
            "states": dict(totals["states"]),
            "runners": dict(totals["runners"]),
        },
    }
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n{totals['jobs']} jobs, {totals['runtime_seconds'] / 3600:.2f} tool-hours, "
          f"states {dict(totals['states'])}, runners {dict(totals['runners'])}")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
