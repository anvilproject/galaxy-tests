"""Delete raw per-run artifacts (deployment logs, results.json) older than
RETENTION_DAYS from reports/anvil/{deployments,tool-tests}/.

Why: these are diagnostic artifacts - useful for digging into a specific
run shortly after it happens, not something anyone browses months later.
Left uncommitted-forever, they're the dominant driver of this repo's
clone/checkout size (~43MB/run before the compression fixes landed
alongside this script, ~daily cadence - unbounded growth). Every run's
own workflow artifact upload (results/ and logs/, kept independently by
GitHub for 90 days by default) already covers "I need to look at last
week's raw log," so there's no loss of near-term access.

Deliberately does NOT touch anything under docs/ (raster-data,
analysis-data, parallelism-data, job-runtime-data) or
reports/anvil/deployments.{json,html,svg} - those are the small, derived,
aggregated data that actually power the historical dashboards, kept
forever by design (see anvil_generate_raster_data.py's own docstring:
"does not filter to a recent window... since the raster's prev/next
navigation and deep links can reach any run"). Pruning raw artifacts
doesn't remove a run from any dashboard - only the raw backing logs.

Usage: anvil_prune_old_raw_reports.py [retention_days]
"""

import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

RETENTION_DAYS = 30

DEPLOYMENTS_DIR = "reports/anvil/deployments"
TOOL_TESTS_DIR = "reports/anvil/tool-tests"

RUN_ID_TIMESTAMP_RE = re.compile(r"-(\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$")


def run_timestamp(run_id: str) -> datetime | None:
    m = RUN_ID_TIMESTAMP_RE.search(run_id)
    if not m:
        return None
    yy, mm, dd, hh, mi, ss = (int(g) for g in m.groups())
    try:
        return datetime(2000 + yy, mm, dd, hh, mi, ss)
    except ValueError:
        return None


def dir_size(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def prune(base_dir: str, cutoff: datetime) -> tuple[int, int]:
    if not os.path.isdir(base_dir):
        return 0, 0
    pruned_count = 0
    pruned_bytes = 0
    for run_id in sorted(os.listdir(base_dir)):
        run_path = os.path.join(base_dir, run_id)
        if not os.path.isdir(run_path):
            continue
        ts = run_timestamp(run_id)
        if ts is None or ts >= cutoff:
            continue  # unparseable run_id or too recent - leave alone
        pruned_bytes += dir_size(run_path)
        shutil.rmtree(run_path)
        pruned_count += 1
    return pruned_count, pruned_bytes


def main() -> None:
    retention_days = int(sys.argv[1]) if len(sys.argv) > 1 else RETENTION_DAYS
    # naive, to compare directly against run_timestamp()'s naive-UTC values
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)

    total_count = 0
    total_bytes = 0
    for base_dir in (DEPLOYMENTS_DIR, TOOL_TESTS_DIR):
        count, freed = prune(base_dir, cutoff)
        total_count += count
        total_bytes += freed

    print(
        f"Pruned {total_count} run(s) older than {retention_days} days "
        f"(cutoff {cutoff.isoformat()}Z), freeing {total_bytes / 1e6:.1f} MB"
    )


if __name__ == "__main__":
    main()
