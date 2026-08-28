"""Backfill missing job_metrics on GCP Batch jobs in a tool-test results.json.

galaxy-tool-test snapshots each job's full details (including job_metrics)
via one API call the instant it sees the job's state flip to "ok", then
moves on - no retry. Metrics collection itself is a separate, deferred
step dispatched to the same job-handler worker pool that's busy finishing
every other job, so its timing relative to the state flip is inconsistent
under load (confirmed live: anywhere from ~14ms to ~12s after state=ok,
routinely landing after galaxy-tool-test's snapshot already fired). The
result: almost every GCP Batch job in a run shows job_metrics: [] in
results.json even though Galaxy finishes collecting them into the DB
moments later - the same class of gap actimeo=1 closed for job output
collection (see galaxy-k8s-boot's "Shrink NFS attribute-cache staleness
window" commit), but at the API-snapshot layer instead of the NFS layer,
so that fix doesn't reach it.

This is the retry_job_output_collection equivalent for job_metrics: after
the test run finishes (and the cluster is still up), re-fetch full job
details for every GCP Batch job that came back with empty job_metrics,
across a few rounds with a short sleep between them, and patch them into
results.json in place before the report/dashboards are built from it.

Usage: anvil_backfill_job_metrics.py <results.json path>
Env: GALAXY_URL, KEY (Galaxy API key)
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

MAX_ROUNDS = 5
SLEEP_SECONDS = 15
MAX_WORKERS = 16


def is_gcp_batch(job: dict) -> bool:
    """Same heuristic as anvil_generate_job_runtime_data.py - not keyed on
    the Batch id prefix since it's configurable (TPV's gcp_batch
    job_id_prefix). Local runner jobs are a bare pid, Kubernetes jobs are
    "gxy-"-prefixed; everything else is Batch."""
    external_id = job.get("external_id") or ""
    return bool(external_id) and not external_id.startswith("gxy-") and not external_id.isdigit()


def fetch_job(session: requests.Session, galaxy_url: str, key: str, job_id: str):
    try:
        resp = session.get(
            f"{galaxy_url}api/jobs/{job_id}",
            params={"full": "true"},
            headers={"x-api-key": key},
            timeout=30,
        )
        resp.raise_for_status()
        return job_id, resp.json()
    except requests.RequestException as e:
        print(f"  fetch failed for job {job_id}: {e}")
        return job_id, None


def main() -> None:
    results_path = sys.argv[1] if len(sys.argv) > 1 else "results/results.json"
    if not os.path.isfile(results_path):
        print(f"{results_path} not found - nothing to backfill")
        return

    galaxy_url = os.environ["GALAXY_URL"]
    key = os.environ["KEY"]

    with open(results_path) as f:
        report = json.load(f)

    tests = report.get("tests", [])
    missing = {}  # job_id -> test dict
    batch_total = 0
    for t in tests:
        job = t.get("data", {}).get("job")
        if not job or not is_gcp_batch(job):
            continue
        batch_total += 1
        if not job.get("job_metrics"):
            missing[job["id"]] = t

    if not missing:
        print(f"{batch_total} GCP Batch job(s), none missing job_metrics - nothing to backfill")
        return

    initially_missing = len(missing)
    print(f"{batch_total} GCP Batch job(s), {initially_missing} missing job_metrics - backfilling")

    session = requests.Session()
    for round_num in range(1, MAX_ROUNDS + 1):
        job_ids = list(missing.keys())
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            results = pool.map(lambda jid: fetch_job(session, galaxy_url, key, jid), job_ids)

        backfilled_this_round = 0
        for job_id, job in results:
            if job and job.get("job_metrics"):
                missing[job_id]["data"]["job"] = job
                del missing[job_id]
                backfilled_this_round += 1

        print(f"  round {round_num}/{MAX_ROUNDS}: backfilled {backfilled_this_round}, {len(missing)} still missing")

        if not missing or round_num == MAX_ROUNDS:
            break
        time.sleep(SLEEP_SECONDS)

    backfilled = initially_missing - len(missing)
    print(f"Backfilled {backfilled}/{initially_missing}; {len(missing)} still missing job_metrics after {MAX_ROUNDS} round(s)")

    if backfilled:
        with open(results_path, "w") as f:
            json.dump(report, f)
        print(f"Updated {results_path}")


if __name__ == "__main__":
    main()
