"""Regenerate docs/job-runtime-data/runs/<run_id>.json: a per-job runtime
record for every GCP Batch job in a run, for the deploy-stages.html
"Job runtimes" Gantt-style chart (one thin bar per job, positioned at its
actual start time within the run).

Restricted to GCP Batch jobs only, per the chart's scope - the local k8s
runner's jobs are numerous and short (see anvil_generate_parallelism_data.py's
own timing comparison) and aren't what this chart is for.

Built from the same job.job_metrics entries anvil_generate_parallelism_data.py
already reads for the queued/running split (start_epoch, end_epoch,
runtime_seconds), plus galaxy_slots/galaxy_memory_mb/container_id for the
chart's hover detail. Coverage is partial: job_metrics only exists for jobs
that ran to completion (see that script's docstring) - roughly 60% of GCP
Batch jobs in a typical run.

start/end in the output are the *overall* run's (across every destination,
not just gcp_batch) - the same values anvil_generate_parallelism_data.py
computes - so this chart's x-axis lines up with the parallelism chart's when
both are looking at the same run.

Usage: anvil_generate_job_runtime_data.py
"""

import json
import os
from datetime import datetime, timezone

TOOL_TESTS_DIR = "reports/anvil/tool-tests"
JOB_RUNTIME_DATA_DIR = "docs/job-runtime-data"
RUNS_DIR = f"{JOB_RUNTIME_DATA_DIR}/runs"


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


def is_gcp_batch(job: dict) -> bool:
    return (job.get("external_id") or "").startswith("galaxy-batch-")


def get_metric(job: dict, name: str):
    for m in job.get("job_metrics", []):
        if m.get("name") == name:
            return m.get("raw_value")
    return None


def epoch_to_iso(raw_value) -> str | None:
    try:
        epoch = float(raw_value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat()


def main() -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    written = 0
    for run_id in all_run_dirs():
        tests = load_tests(run_id)

        # Overall run start/end across every destination, matching
        # anvil_generate_parallelism_data.py's definition, so the two
        # charts share the same x-axis for the same run.
        all_times = []
        jobs = []
        batch_total = 0
        for t in tests:
            job = t.get("data", {}).get("job")
            if not job:
                continue
            ct, ut = job.get("create_time"), job.get("update_time")
            if ct and ut:
                all_times.append(datetime.fromisoformat(ct))
                all_times.append(datetime.fromisoformat(ut))

            if not is_gcp_batch(job):
                continue
            batch_total += 1

            start_iso = epoch_to_iso(get_metric(job, "start_epoch"))
            end_iso = epoch_to_iso(get_metric(job, "end_epoch"))
            runtime = get_metric(job, "runtime_seconds")
            if not start_iso or not end_iso or runtime is None:
                continue

            jobs.append(
                {
                    "tool_id": t.get("data", {}).get("tool_id"),
                    "version": t.get("data", {}).get("tool_version"),
                    "start": start_iso,
                    "end": end_iso,
                    "runtime_seconds": float(runtime),
                    "cores": float(get_metric(job, "galaxy_slots")) if get_metric(job, "galaxy_slots") else None,
                    "memory_mb": float(get_metric(job, "galaxy_memory_mb")) if get_metric(job, "galaxy_memory_mb") else None,
                    "container_id": get_metric(job, "container_id"),
                }
            )

        if not all_times or not jobs:
            continue

        jobs.sort(key=lambda j: j["start"])

        out_path = os.path.join(RUNS_DIR, f"{run_id}.json")
        with open(out_path, "w") as f:
            json.dump(
                {
                    "run_id": run_id,
                    "start": min(all_times).isoformat(),
                    "end": max(all_times).isoformat(),
                    "gcp_batch_jobs_total": batch_total,
                    "jobs": jobs,
                },
                f,
                separators=(",", ":"),
            )
        written += 1

    print(f"Wrote {written} run job-runtime files under {RUNS_DIR}/")


if __name__ == "__main__":
    main()
