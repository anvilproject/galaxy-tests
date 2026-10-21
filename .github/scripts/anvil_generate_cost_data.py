#!/usr/bin/env python3
"""Record per-run GCP compute costs in docs/cost-data/ from Rainstone's API.

Queries the Rainstone service deployed alongside Galaxy on the test VM
(http://<vm-ip>/galaxy/costs/api) before the VM is deleted:

1. Polls GET /api/summary until Rainstone reports no in-progress or
   incomplete jobs and a complete main-VM launch record (or until a
   bounded timeout expires, in which case whatever Rainstone reported is
   recorded as-is).
2. Pins a single calculation revision (revision_id) across GET /api/summary,
   GET /api/jobs?runner=gcp_batch, and concurrent GET /api/jobs/{id}
   requests, restarting from step 1 if the collector supersedes the
   revision mid-load (HTTP 409).
3. Verifies internal consistency of Rainstone's response (logging workflow
   warnings on any discrepancy) and writes:
   - docs/cost-data/runs/<run_id>.json  (main VM + per-Batch-job detail)
   - docs/cost-data/costs.json          (compact per-run summary series)

All cost figures are GCP list-price compute (vCPU + RAM) in USD, copied
directly from Rainstone without local rate tables or disk estimates. Each
Batch job's boot / run / idle / teardown seconds and costs are Rainstone's
own resource phases (`resources[].phases`, `full_phase_totals`); when
Rainstone cannot separate a job's phases, its reason is recorded instead.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_WAIT_SECONDS = 600
DEFAULT_POLL_INTERVAL = 15
DEFAULT_WORKERS = 8
MAX_REVISION_RETRIES = 5
PAGE_LIMIT = 200


class HttpStatusError(Exception):
    def __init__(self, status: int, url: str, detail: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {detail}")
        self.status = status
        self.url = url
        self.detail = detail


def warn(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}", file=sys.stderr)
    else:
        print(f"WARNING: {message}", file=sys.stderr)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_decimal(val: object) -> Decimal | None:
    if val is None or val == "":
        return None
    return Decimal(str(val))


def round_money(val: Decimal | None, places: int = 4) -> float | None:
    if val is None:
        return None
    return round(float(val), places)


def build_headers(cli_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    tenant = os.environ.get("RAINSTONE_TENANT")
    user = os.environ.get("RAINSTONE_USER")
    admin = os.environ.get("RAINSTONE_ADMIN")
    if tenant:
        headers["X-Rainstone-Tenant"] = tenant
    if user:
        headers["X-Rainstone-User"] = user
    if admin:
        headers["X-Rainstone-Admin"] = admin
    for item in cli_headers:
        sep = ":" if ":" in item else "="
        if sep not in item:
            raise ValueError(f"Invalid header {item!r}; expected KEY:VALUE or KEY=VALUE")
        k, v = item.split(sep, 1)
        headers[k.strip()] = v.strip()
    return headers


def api_get(
    base_url: str,
    path: str,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise HttpStatusError(exc.code, url, body or str(exc.reason)) from exc


def resolve_window(
    base_url: str,
    headers: dict[str, str],
    deploy_start_epoch: str | None,
    from_arg: str | None,
    to_arg: str | None,
) -> tuple[str, str | None]:
    """Return (from_iso, fixed_to_iso) with explicit UTC offsets.

    In CI, DEPLOY_START_EPOCH sets `from` and `to` is refreshed to current UTC
    on each poll/attempt. When running against an imported snapshot without
    DEPLOY_START_EPOCH, fall back to the snapshot's own current_launch window.
    """
    if from_arg:
        dt_from = parse_iso(from_arg)
        if not dt_from:
            raise ValueError(f"Cannot parse --from timestamp: {from_arg!r}")
        from_iso = dt_from.astimezone(timezone.utc).isoformat()
    elif deploy_start_epoch:
        dt_from = datetime.fromtimestamp(float(deploy_start_epoch), tz=timezone.utc)
        from_iso = dt_from.isoformat()
    else:
        probe = api_get(
            base_url,
            "/summary",
            params={"mode": "accrued", "timezone": "UTC"},
            headers=headers,
        )
        launch = probe.get("current_launch") or {}
        launch_at = parse_iso(launch.get("launch_at") or launch.get("first_observed_at"))
        as_of = parse_iso(launch.get("as_of") or probe.get("as_of"))
        if launch_at:
            from_iso = (launch_at - timedelta(minutes=5)).astimezone(timezone.utc).isoformat()
        else:
            from_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        if not to_arg and as_of:
            to_arg = (as_of + timedelta(minutes=5)).astimezone(timezone.utc).isoformat()

    fixed_to_iso = None
    if to_arg:
        dt_to = parse_iso(to_arg)
        if not dt_to:
            raise ValueError(f"Cannot parse --to timestamp: {to_arg!r}")
        fixed_to_iso = dt_to.astimezone(timezone.utc).isoformat()

    return from_iso, fixed_to_iso


def build_query(from_iso: str, fixed_to_iso: str | None, revision: str | None = None) -> dict[str, str]:
    to_iso = fixed_to_iso or (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    query = {
        "mode": "accrued",
        "timezone": "UTC",
        "from": from_iso,
        "to": to_iso,
        "sort": "amount",
        "direction": "desc",
    }
    if revision:
        query["revision"] = revision
    return query


def is_complete(summary: dict) -> bool:
    in_progress = summary.get("in_progress_job_count")
    incomplete = (summary.get("coverage") or {}).get("incomplete")
    launch_completeness = (summary.get("current_launch") or {}).get("completeness")
    return (
        in_progress == 0
        and incomplete == 0
        and launch_completeness == "complete"
    )


def wait_for_completeness(
    base_url: str,
    headers: dict[str, str],
    from_iso: str,
    fixed_to_iso: str | None,
    wait_seconds: int,
    poll_interval: int,
) -> dict:
    deadline = time.monotonic() + max(0, wait_seconds)
    attempt = 0
    while True:
        attempt += 1
        query = build_query(from_iso, fixed_to_iso)
        summary = api_get(base_url, "/summary", params=query, headers=headers)
        in_progress = summary.get("in_progress_job_count")
        incomplete = (summary.get("coverage") or {}).get("incomplete")
        launch_comp = (summary.get("current_launch") or {}).get("completeness")
        rev = summary.get("revision_id")
        print(
            f"[poll {attempt}] revision={rev} in_progress={in_progress} "
            f"coverage.incomplete={incomplete} current_launch.completeness={launch_comp}"
        )
        if is_complete(summary):
            print("Rainstone reports complete cost coverage.")
            return summary
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            warn(
                f"Timed out after {wait_seconds}s waiting for Rainstone completeness "
                f"(in_progress={in_progress}, coverage.incomplete={incomplete}, "
                f"current_launch.completeness={launch_comp}); recording current state."
            )
            return summary
        time.sleep(min(poll_interval, max(1.0, remaining)))


def fetch_all_batch_jobs(
    base_url: str,
    headers: dict[str, str],
    pinned_query: dict[str, str],
) -> tuple[list[dict], dict]:
    items: list[dict] = []
    meta: dict | None = None

    offset = 0
    while True:
        params = {
            **pinned_query,
            "runner": "gcp_batch",
            "limit": str(PAGE_LIMIT),
            "offset": str(offset),
        }
        page = api_get(base_url, "/jobs", params=params, headers=headers)
        if meta is None:
            meta = page["meta"]
        batch = page.get("items") or []
        items.extend(batch)
        offset += len(batch)
        if offset >= page.get("total", 0) or not batch:
            break

    assert meta is not None
    return items, meta


def load_pinned_revision(
    base_url: str,
    headers: dict[str, str],
    from_iso: str,
    fixed_to_iso: str | None,
    initial_summary: dict | None,
    workers: int,
) -> tuple[dict, dict, list[dict]]:
    summary = initial_summary
    for attempt in range(1, MAX_REVISION_RETRIES + 1):
        try:
            if summary is None:
                query = build_query(from_iso, fixed_to_iso)
                summary = api_get(base_url, "/summary", params=query, headers=headers)
            revision_id = summary.get("revision_id")
            pinned_query = build_query(from_iso, fixed_to_iso, revision=revision_id)

            job_items, jobs_meta = fetch_all_batch_jobs(
                base_url, headers, pinned_query
            )

            def fetch_one(job_id: str) -> dict:
                params = {"revision": revision_id} if revision_id else {}
                return api_get(base_url, f"/jobs/{job_id}", params=params, headers=headers)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                job_details = list(pool.map(fetch_one, [it["id"] for it in job_items]))

            return summary, jobs_meta, job_details
        except HttpStatusError as exc:
            if exc.status == 409 and attempt < MAX_REVISION_RETRIES:
                print(
                    f"Revision was superseded during load (HTTP 409); "
                    f"retrying ({attempt}/{MAX_REVISION_RETRIES})..."
                )
                summary = None
                continue
            raise
    raise RuntimeError(f"Failed to load a consistent revision after {MAX_REVISION_RETRIES} attempts")


def batch_job_id_for_resource(resource: dict) -> str | None:
    """Extract the GCP Batch job ID for joining against BigQuery billing export.

    Rainstone's `resources[].resource_key` is `gce:<project>/<zone>/<instance_id>`,
    whereas BigQuery's billing export leaves `resource.name` NULL on Batch VMs
    and identifies them by the `batch-job-id` label (stored by Rainstone under
    `resources[].provisioned`).
    """
    prov = resource.get("provisioned") or {}
    labels = prov.get("provider_labels") or {}
    if labels.get("batch-job-id"):
        return str(labels["batch-job-id"])
    batch_name = prov.get("batch_name") or ""
    if "/jobs/" in batch_name:
        return batch_name.rsplit("/jobs/", 1)[-1]
    return None


# Rainstone phase name -> field prefix in runs/<run_id>.json. The prefixes
# predate Rainstone and are kept so older run files render the same way.
PHASE_PREFIXES = {
    "provisioning": "boot",
    "execution": "run",
    "idle": "idle",
    "teardown": "teardown",
}


def phase_fields(detail: dict, resources: list[dict]) -> dict:
    """Per-phase seconds and costs exactly as Rainstone reports them.

    Seconds sum each phase's `observed_seconds` across the job's machines;
    costs are the job's `full_phase_totals`, recorded only when Rainstone
    marks the total complete. A phase Rainstone omits is absent (null), not
    zero. When any machine's phases could not be separated, Rainstone's
    reason is kept and no split is recorded for the job.
    """
    phases = [p for r in resources for p in (r.get("phases") or [])]
    totals = {t["phase"]: t for t in detail.get("full_phase_totals") or []}
    separated = (
        bool(resources)
        and all(r.get("phases") for r in resources)
        and not any(p.get("phase") == "unattributed" for p in phases)
    )
    fields: dict = {"phases_separated": separated}
    for phase, prefix in PHASE_PREFIXES.items():
        matching = [p for p in phases if p.get("phase") == phase]
        seconds = None
        if separated and matching and all(p.get("observed_seconds") is not None for p in matching):
            seconds = round(sum(float(p["observed_seconds"]) for p in matching), 1)
        total = totals.get(phase)
        cost = to_decimal(total.get("amount")) if separated and total and total.get("complete") else None
        fields[f"{prefix}_seconds"] = seconds
        fields[f"{prefix}_cost"] = round_money(cost, 6)
    reasons = sorted({
        p["reason"] for p in phases if p.get("phase") == "unattributed" and p.get("reason")
    })
    fields["phases_reason"] = "; ".join(reasons) or None
    return fields


def map_job_detail(detail: dict) -> dict:
    resources = detail.get("resources") or []
    attempts = detail.get("attempts") or []

    # Billing join keys (`batch-job-id` label values in BigQuery), falling back
    # to Rainstone's resource_key if no Batch metadata is present.
    resource_keys: list[str] = []
    gce_resource_keys: list[str] = []
    for r in resources:
        rk = r.get("resource_key")
        if rk and rk not in gce_resource_keys:
            gce_resource_keys.append(rk)
        bid = batch_job_id_for_resource(r) or rk
        if bid and bid not in resource_keys:
            resource_keys.append(bid)

    res_starts = [r["resource_started_at"] for r in resources if r.get("resource_started_at")]
    res_ends = [r["resource_finished_at"] for r in resources if r.get("resource_finished_at")]
    resource_started_at = min(res_starts) if res_starts else None
    resource_finished_at = (
        max(res_ends)
        if res_starts and len(res_ends) == len(res_starts)
        else None
    )

    phases = phase_fields(detail, resources)
    executions = [
        p for r in resources for p in (r.get("phases") or []) if p.get("phase") == "execution"
    ]
    if phases["phases_separated"] and executions:
        tool_started_at = min(p["started_at"] for p in executions)
        tool_finished_at = max(p["finished_at"] for p in executions)
    else:
        # Only positions the job's row on the dashboard timeline; without
        # Rainstone's phases, no boot/run/teardown split is recorded.
        provider_attempts = [a for a in attempts if a.get("role") != "observation"]
        active_attempts = provider_attempts or attempts
        tool_starts = [a["tool_started_at"] for a in active_attempts if a.get("tool_started_at")]
        tool_ends = [a["tool_finished_at"] for a in active_attempts if a.get("tool_finished_at")]
        tool_started_at = min(tool_starts) if tool_starts else detail.get("started_at")
        tool_finished_at = max(tool_ends) if tool_ends else detail.get("finished_at")

    primary = resources[0] if resources else {}
    full_amount = to_decimal(detail.get("full_job_amount"))

    return {
        "id": detail.get("id"),
        "source_id": detail.get("source_id"),
        "name": resource_keys[0] if resource_keys else detail.get("source_id"),
        "tool_id": detail.get("tool_id"),
        "tool_name": detail.get("tool_name"),
        "tool_version": detail.get("tool_version"),
        "machine_type": primary.get("machine_type"),
        "machine_capacity": primary.get("machine_capacity"),
        "resource_keys": resource_keys,
        "gce_resource_keys": gce_resource_keys,
        "resource_started_at": resource_started_at,
        "tool_started_at": tool_started_at,
        "tool_finished_at": tool_finished_at,
        "resource_finished_at": resource_finished_at,
        **phases,
        "cost": round_money(full_amount, 6),
        "quality": detail.get("full_quality"),
    }


def check_consistency(jobs_meta: dict, job_details: list[dict], mapped_jobs: list[dict]) -> None:
    coverage = jobs_meta.get("coverage") or {}
    expected_jobs = coverage.get("jobs", 0)
    if len(mapped_jobs) != expected_jobs:
        warn(
            f"Rainstone consistency check: len(jobs) ({len(mapped_jobs)}) "
            f"!= jobs.meta.coverage.jobs ({expected_jobs})."
        )

    phase_mismatches = []
    for detail in job_details:
        totals = detail.get("full_phase_totals") or []
        full_amount = to_decimal(detail.get("full_job_amount"))
        if not totals or full_amount is None or not all(t.get("complete") for t in totals):
            continue
        phase_sum = sum((to_decimal(t.get("amount")) or Decimal("0") for t in totals), Decimal("0"))
        if abs(phase_sum - full_amount) > Decimal("0.000001"):
            phase_mismatches.append(detail.get("id"))
    if phase_mismatches:
        warn(
            f"Rainstone consistency check: {len(phase_mismatches)} jobs' phase totals "
            f"do not sum to full_job_amount (e.g. {', '.join(map(str, phase_mismatches[:3]))})."
        )

    priced_jobs = [j for j in mapped_jobs if j.get("cost") is not None]
    expected_priced = coverage.get("priced", 0)
    if len(priced_jobs) != expected_priced:
        warn(
            f"Rainstone consistency check: {len(priced_jobs)} jobs have "
            f"non-null cost, but jobs.meta.coverage.priced is {expected_priced}."
        )

    sum_cost = sum(float(j["cost"]) for j in priced_jobs)
    subtotal_raw = jobs_meta.get("priced_subtotal")
    subtotal = float(subtotal_raw) if subtotal_raw is not None else 0.0
    if abs(sum_cost - subtotal) > 0.01:
        warn(
            f"Rainstone consistency check: sum of job costs ({sum_cost:.6f}) "
            f"differs from jobs.meta.priced_subtotal ({subtotal_raw}) by > $0.01."
        )


def build_main_vm(summary: dict) -> dict:
    launch = summary.get("current_launch") or {}
    elapsed = to_decimal(launch.get("elapsed_seconds"))
    rate = to_decimal(launch.get("hourly_rate"))
    total = to_decimal(launch.get("total_since_launch"))
    return {
        "machine_type": launch.get("machine_type"),
        "machine_capacity": launch.get("machine_capacity"),
        "launch_at": launch.get("launch_at"),
        "as_of": launch.get("as_of"),
        "elapsed_seconds": round(float(elapsed), 1) if elapsed is not None else None,
        "hourly_rate": float(rate) if rate is not None else None,
        "cost": round_money(total, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Harness run identifier (e.g. anvil-test-ci-261021-115117)")
    parser.add_argument(
        "--rainstone-url",
        default=os.environ.get("RAINSTONE_API_URL", "http://localhost:5173/api"),
        help="Base URL of Rainstone's API (default: $RAINSTONE_API_URL or http://localhost:5173/api)",
    )
    parser.add_argument(
        "--github-run-id",
        default=os.environ.get("GITHUB_RUN_ID"),
        help="GitHub Actions run ID for main-VM billing reconciliation (default: $GITHUB_RUN_ID)",
    )
    parser.add_argument(
        "--deploy-start-epoch",
        default=os.environ.get("DEPLOY_START_EPOCH"),
        help="Run start Unix epoch seconds (default: $DEPLOY_START_EPOCH)",
    )
    parser.add_argument("--from", dest="from_time", help="Explicit UTC ISO-8601 start time override")
    parser.add_argument("--to", dest="to_time", help="Explicit UTC ISO-8601 end time override")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=int(os.environ.get("RAINSTONE_WAIT_SECONDS", str(DEFAULT_WAIT_SECONDS))),
        help=f"Max seconds to wait for Rainstone completeness (default: {DEFAULT_WAIT_SECONDS})",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=int(os.environ.get("RAINSTONE_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL))),
        help=f"Seconds between completeness polls (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Concurrent workers for GET /api/jobs/{{id}} (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--header",
        "--headers",
        dest="headers",
        action="append",
        default=[],
        help="Extra HTTP header KEY:VALUE (repeatable; for local development stacks)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/cost-data"),
        help="Output directory for costs.json and runs/<run_id>.json (default: docs/cost-data)",
    )
    args = parser.parse_args()

    headers = build_headers(args.headers)
    from_iso, fixed_to_iso = resolve_window(
        args.rainstone_url,
        headers,
        args.deploy_start_epoch,
        args.from_time,
        args.to_time,
    )

    initial_summary = wait_for_completeness(
        args.rainstone_url,
        headers,
        from_iso,
        fixed_to_iso,
        args.wait_seconds,
        args.poll_interval,
    )

    summary, jobs_meta, job_details = load_pinned_revision(
        args.rainstone_url,
        headers,
        from_iso,
        fixed_to_iso,
        initial_summary,
        args.workers,
    )

    revision_id = summary.get("revision_id")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output and revision_id:
        with open(github_output, "a", encoding="utf-8") as out_f:
            out_f.write(f"revision_id={revision_id}\n")

    mapped_jobs = [map_job_detail(d) for d in job_details]
    check_consistency(jobs_meta, job_details, mapped_jobs)
    mapped_jobs.sort(
        key=lambda j: (
            j.get("resource_started_at") is None,
            j.get("resource_started_at") or "",
            j.get("tool_started_at") or "",
            j.get("id") or "",
        )
    )

    main_vm = build_main_vm(summary)
    launch = summary.get("current_launch") or {}
    main_vm_dec = to_decimal(launch.get("total_since_launch"))

    coverage = jobs_meta.get("coverage") or {}
    gcp_batch_jobs_total = int(coverage.get("jobs", 0))
    gcp_batch_jobs_priced = int(coverage.get("priced", 0))
    subtotal_raw = jobs_meta.get("priced_subtotal")
    if subtotal_raw is not None:
        gcp_batch_dec = to_decimal(subtotal_raw)
    elif gcp_batch_jobs_total == 0 and int(coverage.get("incomplete", 0)) == 0:
        gcp_batch_dec = Decimal("0")
    else:
        gcp_batch_dec = None

    total_dec = (
        main_vm_dec + gcp_batch_dec
        if main_vm_dec is not None and gcp_batch_dec is not None
        else None
    )

    launch_dt = parse_iso(launch.get("launch_at")) or parse_iso(from_iso)
    timestamp = (
        launch_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if launch_dt
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    )

    main_vm_cost = round_money(main_vm_dec, 4)
    gcp_batch_cost = round_money(gcp_batch_dec, 4)
    total_cost = round_money(total_dec, 4)

    run_detail: dict = {
        "run_id": args.run_id,
        "timestamp": timestamp,
        "rainstone_revision": revision_id,
        "main_vm": main_vm,
        "gcp_batch_jobs_total": gcp_batch_jobs_total,
        "gcp_batch_jobs_priced": gcp_batch_jobs_priced,
        "gcp_batch_total_cost": gcp_batch_cost,
        "total_cost": total_cost,
        "jobs": mapped_jobs,
    }
    if args.github_run_id:
        run_detail["github_run_id"] = str(args.github_run_id)

    runs_dir = args.output_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    detail_path = runs_dir / f"{args.run_id}.json"
    detail_path.write_text(json.dumps(run_detail, separators=(",", ":")))

    costs_path = args.output_dir / "costs.json"
    existing = json.loads(costs_path.read_text()) if costs_path.exists() else {"runs": []}
    runs = [r for r in existing.get("runs", []) if r.get("run_id") != args.run_id]
    run_summary: dict = {
        "run_id": args.run_id,
        "timestamp": timestamp,
        "main_vm_cost": main_vm_cost,
        "gcp_batch_cost": gcp_batch_cost,
        "gcp_batch_jobs_priced": gcp_batch_jobs_priced,
        "gcp_batch_jobs_total": gcp_batch_jobs_total,
        "total_cost": total_cost,
        "rainstone_revision": revision_id,
    }
    if args.github_run_id:
        run_summary["github_run_id"] = str(args.github_run_id)
    runs.append(run_summary)
    runs.sort(key=lambda r: r.get("timestamp", ""))
    costs_path.write_text(json.dumps({"runs": runs}, separators=(",", ":")))

    print(
        f"{args.run_id}: main VM = ${main_vm_cost} "
        f"({main_vm.get('machine_type')}, {main_vm.get('elapsed_seconds')}s), "
        f"GCP Batch = ${gcp_batch_cost} "
        f"({gcp_batch_jobs_priced}/{gcp_batch_jobs_total} jobs priced), "
        f"total = ${total_cost} (revision={revision_id})"
    )


if __name__ == "__main__":
    main()
