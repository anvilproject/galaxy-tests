"""Generate docs/cost-data/{costs.json,runs/<run_id>.json}: a best-effort
GCP cost estimate for one CI run, computed while the main VM and this
run's GCP Batch job records are still live/queryable.

Unlike every other docs/*-data generator, this canNOT be recomputed
later from committed report data alone - GCP Batch job records and the
main VM's own live specs eventually age out - so it runs once, at the
end of the run that produced it, and APPENDS to history rather than
rescanning it (same shape as anvil_record_deployment.py, not the
raster/analysis/job-runtime family, which rebuild their full output from
scratch every run).

Methodology (see the 2026-09 investigation that produced these numbers -
ea-no-commit/talk-outline.md slide 11, not committed here). The unit
prices below are REAL billed rates for this GCP project, derived once
from its own BigQuery billing export (project anvil-and-terra-development,
dataset daily_cost_detail: cost/usage per Compute Engine SKU on a clean
day) - not public list pricing, and not a live query every run. The CI
service account's access to that BigQuery dataset is unverified, and
list pricing barely moves month to month, so a periodic manual refresh
of the constants below is preferable to a new per-run dependency.
Applied to this run's own exact machine types, boot-disk sizes, and
measured VM lifetimes:

- Main VM: launch (the "Record deploy start time" step's epoch) to "now"
  (this script's own run time) - a few minutes short of the VM's actual
  deletion, since this step necessarily runs before "Delete VM" (the
  commit-and-push step needs this data, and runs before VM teardown).
  Negligible: the main VM is the smaller of the two cost components.
- GCP Batch jobs: each job's own SCHEDULED->terminal status-event window
  (the VM's true billed lifetime) - NOT galaxy-tool-test's reported
  "runtime_seconds", which only covers the RUNNING phase and misses the
  ~90-100s/job of provisioning overhead this per-job-VM architecture
  actually pays for. Split into boot (SCHEDULED->RUNNING, measured) /
  run (RUNNING->terminal, measured) / teardown (a flat
  TEARDOWN_ESTIMATE_SECONDS - GCP Batch exposes no event for when the
  VM is actually deleted after job completion, so this one segment is a
  labeled estimate, not a measurement).

Usage: anvil_generate_cost_data.py <instance_name>
Env: GCP_PROJECT, GCP_ZONE (both already set at the workflow level),
     DEPLOY_START_EPOCH (from the "Record deploy start time" step's
     epoch output)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

COST_DATA_DIR = "docs/cost-data"
COSTS_PATH = f"{COST_DATA_DIR}/costs.json"
RUNS_DIR = f"{COST_DATA_DIR}/runs"

BATCH_LOCATION = "us-east4"  # matches the "Clean up orphaned GCP Batch jobs" step

# Derived 2026-09-02 from this project's own BigQuery billing export -
# see this file's own docstring. Refresh periodically; these are not
# fetched live.
T2D_VCPU_HR = 0.023233
T2D_RAM_GIB_HR = 0.003114
PD_BALANCED_GIB_HR = 0.0001147
N2_VCPU_HR = 0.026705
N2_RAM_GIB_HR = 0.003580
PD_STANDARD_GIB_HR = 0.0000459

# Not measured - see docstring. A GCE instance delete typically completes
# on this order of magnitude; flagged as an estimate everywhere it's used.
TEARDOWN_ESTIMATE_SECONDS = 10


def run_gcloud_json(args: list[str]):
    result = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def n2_specs(machine_type: str):
    fam, size = machine_type.rsplit("-", 1)
    vcpu = int(size)
    ram_per_vcpu = {"n2-standard": 4, "n2-highmem": 8, "n2-highcpu": 1}.get(fam)
    if ram_per_vcpu is None:
        return None
    return vcpu, vcpu * ram_per_vcpu


def t2d_specs(machine_type: str):
    if not machine_type.startswith("t2d-standard-"):
        return None
    vcpu = int(machine_type.rsplit("-", 1)[1])
    return vcpu, vcpu * 4


def get_main_vm_info(instance_name: str, project: str, zone: str):
    data = run_gcloud_json(
        ["compute", "instances", "describe", instance_name, f"--project={project}", f"--zone={zone}"]
    )
    machine_type = data["machineType"].rsplit("/", 1)[-1]
    boot_disk = data["disks"][0]
    return {
        "machine_type": machine_type,
        "boot_disk_gb": float(boot_disk.get("diskSizeGb", 100)),
        "boot_disk_type": boot_disk.get("type", "PERSISTENT"),
    }


def main_vm_cost(info: dict, lifetime_seconds: float) -> dict:
    specs = t2d_specs(info["machine_type"])
    if specs is None:
        print(f"Unrecognized main VM machine type {info['machine_type']!r} - skipping cost calc", file=sys.stderr)
        return {"cost": None}
    vcpu, ram_gib = specs
    hrs = lifetime_seconds / 3600.0
    # boot disk type here is the GCE disk resource's own `type` (e.g.
    # ".../diskTypes/pd-balanced"), not the Batch job JSON's simpler
    # bootDisk.type string - normalize to the same pd-balanced/pd-standard
    # label used for pricing.
    disk_type = "pd-balanced" if "balanced" in info["boot_disk_type"] else "pd-standard"
    disk_rate = PD_BALANCED_GIB_HR if disk_type == "pd-balanced" else PD_STANDARD_GIB_HR
    compute_cost = vcpu * T2D_VCPU_HR * hrs + ram_gib * T2D_RAM_GIB_HR * hrs
    disk_cost = info["boot_disk_gb"] * disk_rate * hrs
    return {
        "machine_type": info["machine_type"],
        "vcpu": vcpu,
        "ram_gib": ram_gib,
        "boot_disk_gb": info["boot_disk_gb"],
        "boot_disk_type": disk_type,
        "lifetime_seconds": round(lifetime_seconds, 1),
        "compute_cost": round(compute_cost, 4),
        "disk_cost": round(disk_cost, 4),
        "cost": round(compute_cost + disk_cost, 4),
    }


def batch_job_cost(job: dict) -> dict | None:
    status = job.get("status", {})
    instances = status.get("taskGroups", {}).get("group0", {}).get("instances", [{}])
    if not instances:
        return None
    machine_type = instances[0].get("machineType")
    boot_gb = float(instances[0].get("bootDisk", {}).get("sizeGb", 100))
    specs = n2_specs(machine_type) if machine_type else None
    if specs is None:
        return None
    vcpu, ram_gib = specs

    scheduled_t = running_t = terminal_t = None
    for e in status.get("statusEvents", []):
        desc = e.get("description", "")
        if "to SCHEDULED" in desc:
            scheduled_t = parse_ts(e["eventTime"])
        elif "to RUNNING" in desc:
            running_t = parse_ts(e["eventTime"])
        elif "to SUCCEEDED" in desc or "to FAILED" in desc:
            terminal_t = parse_ts(e["eventTime"])
    if not scheduled_t or not terminal_t:
        return None
    if not running_t:
        running_t = terminal_t  # no observed RUNNING transition - treat as zero-length boot

    boot_s = max(0.0, (running_t - scheduled_t).total_seconds())
    run_s = max(0.0, (terminal_t - running_t).total_seconds())
    teardown_s = float(TEARDOWN_ESTIMATE_SECONDS)

    def phase_cost(seconds: float) -> float:
        hrs = seconds / 3600.0
        return vcpu * N2_VCPU_HR * hrs + ram_gib * N2_RAM_GIB_HR * hrs + boot_gb * PD_STANDARD_GIB_HR * hrs

    boot_cost, run_cost, teardown_cost = phase_cost(boot_s), phase_cost(run_s), phase_cost(teardown_s)
    labels = job.get("labels", {}) or {}

    return {
        "name": job["name"].rsplit("/", 1)[-1],
        "tool_id": labels.get("galaxy-tool-id"),
        "machine_type": machine_type,
        "vcpu": vcpu,
        "ram_gib": ram_gib,
        "scheduled_at": scheduled_t.isoformat(),
        "running_at": running_t.isoformat(),
        "terminal_at": terminal_t.isoformat(),
        "boot_seconds": round(boot_s, 1),
        "run_seconds": round(run_s, 1),
        "teardown_seconds": teardown_s,
        "boot_cost": round(boot_cost, 5),
        "run_cost": round(run_cost, 5),
        "teardown_cost": round(teardown_cost, 5),
        "cost": round(boot_cost + run_cost + teardown_cost, 5),
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: anvil_generate_cost_data.py <instance_name>")
    # Same string everywhere in this codebase's docs/*-data - the VM
    # instance name doubles as the run identifier.
    run_id = instance_name = sys.argv[1]

    project = os.environ["GCP_PROJECT"]
    zone = os.environ["GCP_ZONE"]
    deploy_start_epoch = int(os.environ["DEPLOY_START_EPOCH"])
    deploy_start = datetime.fromtimestamp(deploy_start_epoch, tz=timezone.utc)
    now = datetime.now(timezone.utc)

    vm_info = get_main_vm_info(instance_name, project, zone)
    lifetime_seconds = (now - deploy_start).total_seconds()
    vm_cost = main_vm_cost(vm_info, lifetime_seconds)

    jobs_raw = run_gcloud_json(
        [
            "batch",
            "jobs",
            "list",
            f"--project={project}",
            f"--location={BATCH_LOCATION}",
            f"--filter=createTime>={deploy_start.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        ]
    ) or []

    batch_jobs = [j for j in (batch_job_cost(j) for j in jobs_raw) if j is not None]
    batch_jobs.sort(key=lambda j: j["scheduled_at"])
    batch_total_cost = round(sum(j["cost"] for j in batch_jobs), 4)

    total_cost = round((vm_cost.get("cost") or 0) + batch_total_cost, 4)

    os.makedirs(RUNS_DIR, exist_ok=True)
    run_detail = {
        "run_id": run_id,
        "timestamp": deploy_start.replace(tzinfo=None).isoformat(),
        "main_vm": vm_cost,
        "gcp_batch_jobs_total": len(jobs_raw),
        "gcp_batch_jobs_priced": len(batch_jobs),
        "gcp_batch_total_cost": batch_total_cost,
        "total_cost": total_cost,
        "teardown_estimate_seconds": TEARDOWN_ESTIMATE_SECONDS,
        "jobs": batch_jobs,
    }
    with open(f"{RUNS_DIR}/{run_id}.json", "w") as f:
        json.dump(run_detail, f, separators=(",", ":"))

    try:
        with open(COSTS_PATH) as f:
            costs = json.load(f)
    except FileNotFoundError:
        costs = {"runs": []}

    costs["runs"] = [r for r in costs["runs"] if r["run_id"] != run_id]
    costs["runs"].append(
        {
            "run_id": run_id,
            "timestamp": run_detail["timestamp"],
            "main_vm_cost": vm_cost.get("cost"),
            "gcp_batch_cost": batch_total_cost,
            "gcp_batch_jobs_priced": len(batch_jobs),
            "gcp_batch_jobs_total": len(jobs_raw),
            "total_cost": total_cost,
        }
    )
    with open(COSTS_PATH, "w") as f:
        json.dump(costs, f, separators=(",", ":"))

    print(
        f"Wrote cost data for {run_id}: main VM ${vm_cost.get('cost') or 0:.2f}, "
        f"GCP Batch ${batch_total_cost:.2f} ({len(batch_jobs)}/{len(jobs_raw)} jobs priced), "
        f"total ${total_cost:.2f}"
    )


if __name__ == "__main__":
    main()
