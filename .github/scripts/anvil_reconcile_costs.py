#!/usr/bin/env python3
"""Reconcile Rainstone's per-run cost estimates against GCP BigQuery billing export.

For each eligible run in `docs/cost-data/costs.json` (or an explicit `--run-id`):
  - Reads `docs/cost-data/runs/<run_id>.json`
  - Queries `anvil-and-terra-development.daily_cost_detail.gcp_billing_export_resource_v1_016D13_3B2B1C_E919A9`
    for compute-only SKUs (`REGEXP_CONTAINS(sku.description, r'Instance (Core|Ram) running in')`)
    using `SUM(cost_at_list)`:
      * Main VM: `labels.github-run-id = <github_run_id>`
      * GCP Batch VMs: `labels.batch-job-id IN <run's resource_keys>`
  - Writes `docs/cost-data/reconciliation/<run_id>.json` with `queried_at`,
    `rainstone_revision`, `main_vm`, `gcp_batch`, `total`, and per-job `jobs[]`.
    The Batch and total comparisons cover only jobs Rainstone priced;
    billing for jobs it left unpriced is under `gcp_batch.unpriced_in_rainstone`.

Numbers are recorded only; discrepancies never fail the workflow.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_BILLING_TABLE = (
    "anvil-and-terra-development.daily_cost_detail."
    "gcp_billing_export_resource_v1_016D13_3B2B1C_E919A9"
)
DEFAULT_GCP_PROJECT = "anvil-and-terra-development"
MIN_AGE_DAYS = 2
MAX_AGE_DAYS = 10


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def round_money(val: float | None, digits: int = 4) -> float | None:
    if val is None:
        return None
    return round(float(val), digits)


def diff_money(rainstone: float | None, billing: float | None, digits: int = 4) -> float | None:
    if rainstone is None or billing is None:
        return None
    return round(float(rainstone) - float(billing), digits)


def infer_run_window(run_summary: dict, run_detail: dict) -> tuple[datetime, datetime]:
    anchor_starts: list[datetime] = []
    for candidate in (
        (run_detail.get("main_vm") or {}).get("launch_at"),
        run_detail.get("timestamp"),
        run_summary.get("timestamp"),
    ):
        dt = parse_iso(candidate)
        if dt:
            anchor_starts.append(dt)

    job_starts: list[datetime] = []
    ends: list[datetime] = []

    for candidate in ((run_detail.get("main_vm") or {}).get("as_of"),):
        dt = parse_iso(candidate)
        if dt:
            ends.append(dt)

    for job in run_detail.get("jobs") or []:
        for k in ("resource_started_at", "scheduled_at", "tool_started_at", "running_at"):
            dt = parse_iso(job.get(k))
            if dt:
                job_starts.append(dt)
        for k in ("resource_finished_at", "terminal_at", "tool_finished_at"):
            dt = parse_iso(job.get(k))
            if dt:
                ends.append(dt)

    starts = anchor_starts or job_starts
    if not starts:
        raise ValueError(f"Cannot determine start timestamp for run {run_detail.get('run_id')}")

    start_dt = min(starts)
    valid_ends = [dt for dt in ends if dt >= start_dt]
    end_dt = max(valid_ends) if valid_ends else (start_dt + timedelta(hours=6))

    # Expand usage_start_time window slightly to cover hourly billing buckets
    # at the edges of the run.
    usage_start = start_dt.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    usage_end = end_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    return usage_start, usage_end


def query_bigquery(
    project: str,
    billing_table: str,
    github_run_id: str,
    resource_keys: list[str],
    usage_start: datetime,
    usage_end: datetime,
) -> tuple[float, dict[str, float]]:
    # Scan partitions from run start day through run end + 3 days inclusive
    # (i.e. < end_day + 4 days), since _PARTITIONTIME is export time.
    partition_start = usage_start.strftime("%Y-%m-%d")
    partition_end = (usage_end.date() + timedelta(days=4)).strftime("%Y-%m-%d")
    usage_start_str = usage_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    usage_end_str = usage_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    sql = f"""
SELECT
  (SELECT l.value FROM UNNEST(labels) AS l WHERE l.key = 'github-run-id' LIMIT 1) AS github_run_id,
  (SELECT l.value FROM UNNEST(labels) AS l WHERE l.key = 'batch-job-id' LIMIT 1) AS batch_job_id,
  SUM(cost_at_list) AS cost_at_list
FROM `{billing_table}`
WHERE _PARTITIONTIME >= TIMESTAMP('{partition_start}')
  AND _PARTITIONTIME < TIMESTAMP('{partition_end}')
  AND usage_start_time >= TIMESTAMP('{usage_start_str}')
  AND usage_start_time <= TIMESTAMP('{usage_end_str}')
  AND REGEXP_CONTAINS(sku.description, r'Instance (Core|Ram) running in')
  AND (
    EXISTS (
      SELECT 1 FROM UNNEST(labels) AS l
      WHERE l.key = 'github-run-id' AND l.value = @github_run_id
    )
    OR EXISTS (
      SELECT 1 FROM UNNEST(labels) AS l
      WHERE l.key = 'batch-job-id' AND l.value IN UNNEST(@resource_keys)
    )
  )
GROUP BY 1, 2
""".strip()

    cmd = [
        "bq",
        "query",
        f"--project_id={project}",
        "--use_legacy_sql=false",
        "--max_rows=100000",
        "--format=json",
        f"--parameter=github_run_id:STRING:{github_run_id}",
        f"--parameter=resource_keys:ARRAY<STRING>:{json.dumps(resource_keys)}",
        sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"bq query failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )

    out = proc.stdout.strip()
    rows = json.loads(out) if out else []

    main_vm_billing = 0.0
    batch_billing_by_key: dict[str, float] = {}
    for row in rows:
        amount = float(row.get("cost_at_list") or 0.0)
        if row.get("github_run_id") == str(github_run_id):
            main_vm_billing += amount
        bkey = row.get("batch_job_id")
        if bkey:
            batch_billing_by_key[bkey] = batch_billing_by_key.get(bkey, 0.0) + amount

    return main_vm_billing, batch_billing_by_key


def select_eligible_runs(
    costs_data: dict,
    reconciliation_dir: Path,
    now: datetime,
    explicit_run_id: str | None,
    force: bool,
) -> list[dict]:
    runs = costs_data.get("runs") or []
    if explicit_run_id:
        matched = [r for r in runs if r.get("run_id") == explicit_run_id]
        if not matched:
            raise ValueError(f"Run {explicit_run_id!r} not found in costs.json")
        return matched

    eligible: list[dict] = []
    for run in runs:
        run_id = run.get("run_id")
        github_run_id = run.get("github_run_id")
        if not run_id or not github_run_id:
            continue
        recon_path = reconciliation_dir / f"{run_id}.json"
        if recon_path.exists() and not force:
            continue
        ts = parse_iso(run.get("timestamp"))
        if not ts:
            continue
        age = now - ts
        if timedelta(days=MIN_AGE_DAYS) <= age <= timedelta(days=MAX_AGE_DAYS):
            eligible.append(run)
    return eligible


def reconcile_one_run(
    run_summary: dict,
    run_detail: dict,
    project: str,
    billing_table: str,
    queried_at: datetime,
) -> dict:
    run_id = run_summary["run_id"]
    github_run_id = str(
        run_summary.get("github_run_id") or run_detail.get("github_run_id") or ""
    )
    if not github_run_id:
        raise ValueError(f"Run {run_id} has no github_run_id")

    usage_start, usage_end = infer_run_window(run_summary, run_detail)

    # Collect resource keys across all Batch jobs in the run.
    all_resource_keys: list[str] = []
    rainstone_by_key: dict[str, float | None] = {}
    for job in run_detail.get("jobs") or []:
        keys = job.get("resource_keys") or ([job["name"]] if job.get("name") else [])
        keys = [k for k in keys if k]
        if not keys:
            continue
        for k in keys:
            if k not in all_resource_keys:
                all_resource_keys.append(k)
        primary_key = keys[0]
        cost_val = job.get("cost")
        rainstone_by_key[primary_key] = float(cost_val) if cost_val is not None else None

    main_vm_billing_raw, batch_billing_by_key = query_bigquery(
        project=project,
        billing_table=billing_table,
        github_run_id=github_run_id,
        resource_keys=all_resource_keys,
        usage_start=usage_start,
        usage_end=usage_end,
    )

    rainstone_priced_keys = {k for k, v in rainstone_by_key.items() if v is not None}
    rainstone_unpriced_keys = set(rainstone_by_key) - rainstone_priced_keys
    billing_present_keys = set(batch_billing_by_key.keys())

    only_in_rainstone = sorted(rainstone_priced_keys - billing_present_keys)
    only_in_billing = sorted(billing_present_keys - rainstone_priced_keys)

    all_compared_keys = sorted(set(rainstone_by_key.keys()) | billing_present_keys)
    job_rows: list[dict] = []
    for key in all_compared_keys:
        r_cost = round_money(rainstone_by_key.get(key), 6)
        b_cost = round_money(batch_billing_by_key.get(key), 6)
        job_rows.append(
            {
                "resource_key": key,
                "rainstone": r_cost,
                "billing": b_cost,
                "difference": diff_money(r_cost, b_cost, 6),
            }
        )

    main_vm_rainstone = round_money(run_summary.get("main_vm_cost"), 4)
    main_vm_billing = round_money(main_vm_billing_raw, 4)

    # Rainstone's Batch figure covers only the jobs it priced, so only their
    # billing is compared with it. Billing for jobs Rainstone left unpriced
    # is recorded beside the comparison: it is cost Rainstone is missing,
    # not an error in the costs it reported.
    batch_rainstone = round_money(run_summary.get("gcp_batch_cost"), 4)
    batch_billing_raw = sum(
        v for k, v in batch_billing_by_key.items() if k in rainstone_priced_keys
    )
    unpriced_billing_raw = sum(
        v for k, v in batch_billing_by_key.items() if k in rainstone_unpriced_keys
    )
    batch_billing = round_money(batch_billing_raw, 4)

    total_rainstone = round_money(run_summary.get("total_cost"), 4)
    total_billing = round_money(main_vm_billing_raw + batch_billing_raw, 4)

    return {
        "run_id": run_id,
        "github_run_id": github_run_id,
        "queried_at": queried_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rainstone_revision": run_detail.get("rainstone_revision")
        or run_summary.get("rainstone_revision"),
        "main_vm": {
            "rainstone": main_vm_rainstone,
            "billing": main_vm_billing,
            "difference": diff_money(main_vm_rainstone, main_vm_billing, 4),
        },
        "gcp_batch": {
            "rainstone": batch_rainstone,
            "billing": batch_billing,
            "difference": diff_money(batch_rainstone, batch_billing, 4),
            "rainstone_jobs": len(rainstone_priced_keys),
            "billing_jobs": len(billing_present_keys & rainstone_priced_keys),
            "only_in_rainstone": only_in_rainstone,
            "only_in_billing": only_in_billing,
            "unpriced_in_rainstone": {
                "jobs": len(rainstone_unpriced_keys),
                "jobs_with_billing": len(rainstone_unpriced_keys & billing_present_keys),
                "billing": round_money(unpriced_billing_raw, 4),
            },
        },
        "total": {
            "rainstone": total_rainstone,
            "billing": total_billing,
            "difference": diff_money(total_rainstone, total_billing, 4),
        },
        "jobs": job_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cost-data-dir",
        type=Path,
        default=Path("docs/cost-data"),
        help="Directory containing costs.json and runs/ (default: docs/cost-data)",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Optional explicit run_id to reconcile regardless of age window",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing reconciliation files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print reconciliation summaries without writing files",
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_GCP_PROJECT,
        help=f"GCP project for bq query (default: {DEFAULT_GCP_PROJECT})",
    )
    parser.add_argument(
        "--billing-table",
        default=DEFAULT_BILLING_TABLE,
        help="BigQuery billing export table",
    )
    args = parser.parse_args()

    costs_path = args.cost_data_dir / "costs.json"
    if not costs_path.exists():
        print(f"No {costs_path} found; nothing to reconcile.")
        return

    costs_data = json.loads(costs_path.read_text())
    reconciliation_dir = args.cost_data_dir / "reconciliation"
    now = datetime.now(timezone.utc)

    explicit = args.run_id.strip() or None
    eligible = select_eligible_runs(
        costs_data=costs_data,
        reconciliation_dir=reconciliation_dir,
        now=now,
        explicit_run_id=explicit,
        force=args.force or bool(explicit),
    )

    if not eligible:
        print("No eligible runs to reconcile.")
        return

    reconciliation_dir.mkdir(parents=True, exist_ok=True)
    for run_summary in eligible:
        run_id = run_summary["run_id"]
        detail_path = args.cost_data_dir / "runs" / f"{run_id}.json"
        if not detail_path.exists():
            print(f"Skipping {run_id}: {detail_path} does not exist", file=sys.stderr)
            continue

        run_detail = json.loads(detail_path.read_text())
        result = reconcile_one_run(
            run_summary=run_summary,
            run_detail=run_detail,
            project=args.project,
            billing_table=args.billing_table,
            queried_at=now,
        )

        vm = result["main_vm"]
        batch = result["gcp_batch"]
        tot = result["total"]
        unpriced = batch["unpriced_in_rainstone"]

        def signed(value: float | None) -> str:
            return "n/a" if value is None else f"{value:+.4f}"

        print(
            f"{run_id} (github_run_id={result['github_run_id']}): "
            f"main_vm rainstone=${vm['rainstone']} billing=${vm['billing']} (diff={signed(vm['difference'])}), "
            f"gcp_batch rainstone=${batch['rainstone']} billing=${batch['billing']} (diff={signed(batch['difference'])}, "
            f"jobs={batch['rainstone_jobs']}/{batch['billing_jobs']}), "
            f"unpriced in rainstone: {unpriced['jobs']} jobs, billing=${unpriced['billing']}, "
            f"total rainstone=${tot['rainstone']} billing=${tot['billing']} (diff={signed(tot['difference'])})"
        )

        if not args.dry_run:
            out_path = reconciliation_dir / f"{run_id}.json"
            out_path.write_text(json.dumps(result, indent=2) + "\n")
            print(f"  wrote {out_path}")


if __name__ == "__main__":
    main()
