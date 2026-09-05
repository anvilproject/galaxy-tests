"""Regenerate docs/raster-data/{manifest,matrix}.json and per-run
docs/raster-data/runs/<run_id>/detail.json from every committed
reports/anvil/tool-tests/<run_id>/results.json - the data feeding the
tool x run event raster (see v2_raster.md / v2_raster_plan.md). Also
copies each run's results.html into docs/tool-tests/<run_id>/ so the
raster's "Full report" links point at a page GitHub Pages serves
directly, rather than a raw results.json blob on github.com that's
too big to render there.

This is the sole generator as of the step-7 cutover (v2_raster_plan.md
sec 6): anvil_generate_heatmap.py and docs/index.md's per-tool table are
retired. It does not filter to a recent window: matrix/manifest rows -
and now the docs/tool-tests/ copies too - are kept for the full run
history rather than pruned to a rolling window, since the raster's
prev/next navigation and deep links can reach any run, not just the
currently-drawn columns. (docs/tool-tests/ growing unbounded is the same
tradeoff already accepted for runs/*/detail.json - revisit only if repo
size actually becomes a problem.)

Row set (tools) grows as tools are actually attempted, not a fixed list -
see v2_raster_plan.md sec 2/7. tools_expected/coverage-against-the-full-
toolset is left out of manifest.json for now: computing it would mean
checking out usegalaxy-tools/cloud/*.yml from this job too, which isn't
wired up yet.

Usage: anvil_generate_raster_data.py
"""

import json
import os
import re
import shutil
from collections import defaultdict

TOOL_TESTS_DIR = "reports/anvil/tool-tests"
DOCS_TOOL_TESTS_DIR = "docs/tool-tests"
RASTER_DATA_DIR = "docs/raster-data"
MANIFEST_PATH = f"{RASTER_DATA_DIR}/manifest.json"
MATRIX_PATH = f"{RASTER_DATA_DIR}/matrix.json"
RUNS_DIR = f"{RASTER_DATA_DIR}/runs"
PAGES_BASE = "https://anvilproject.github.io/galaxy-tests"

ATTEMPTED_STATUSES = {"success", "failure", "error"}
RUN_ID_TIMESTAMP_RE = re.compile(r"-(\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$")


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


def load_run(run_id: str) -> list[dict]:
    path = os.path.join(TOOL_TESTS_DIR, run_id, "results.json")
    with open(path) as f:
        data = json.load(f)
    return data.get("tests", [])


def run_timestamp(run_id: str) -> str | None:
    """Best-effort ISO timestamp parsed from the trailing -YYMMDD-HHMMSS in
    the run id (e.g. anvil-test-ci-260819-030926) - the instance-name
    prefix is a workflow_dispatch input, so it's not fixed, but the
    timestamp suffix launch_vm.sh appends always has this shape."""
    m = RUN_ID_TIMESTAMP_RE.search(run_id)
    if not m:
        return None
    yy, mm, dd, hh, mi, ss = m.groups()
    return f"20{yy}-{mm}-{dd}T{hh}:{mi}:{ss}"


def run_duration_seconds(tests: list[dict]) -> float | None:
    """Approximate the tool-testing phase's wall-clock span as the range
    from the earliest job create_time to the latest job update_time across
    all tests in the run - there's no top-level duration in results.json,
    and this avoids depending on the GitHub Actions API from inside the
    generation script."""
    times = []
    for t in tests:
        job = t.get("data", {}).get("job") or {}
        times.append(job.get("create_time"))
        times.append(job.get("update_time"))
    times = sorted(t for t in times if t)
    if len(times) < 2:
        return None
    from datetime import datetime

    fmt = "%Y-%m-%dT%H:%M:%S.%f"

    def parse(s):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")

    return (parse(times[-1]) - parse(times[0])).total_seconds()


def tool_aggregates(tests: list[dict]) -> dict[str, dict]:
    """tool_id -> {status, affected, versions_affected, versions_total,
    tests_attempted, tests_passed, tests_failed, tests_errored,
    tests_skipped} for one run's flat test list."""
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for t in tests:
        d = t["data"]
        tool_id = d.get("tool_id") or t["id"].rsplit("/", 1)[0]
        by_tool[tool_id].append(d)

    aggregates = {}
    for tool_id, cases in by_tool.items():
        n_success = sum(1 for c in cases if c["status"] == "success")
        n_fail = sum(1 for c in cases if c["status"] == "failure")
        n_error = sum(1 for c in cases if c["status"] == "error")
        n_skip = sum(1 for c in cases if c["status"] == "skip")
        attempted = n_success + n_fail + n_error
        if attempted == 0:
            continue  # skip-only: nothing meaningfully tested this run

        versions_seen: dict[str, bool] = {}
        for c in cases:
            if c["status"] not in ATTEMPTED_STATUSES:
                continue
            version = c.get("tool_version") or ""
            affected = c["status"] in ("failure", "error")
            versions_seen[version] = versions_seen.get(version, False) or affected

        if n_error and n_fail:
            status = "mixed"
        elif n_error:
            status = "error"
        elif n_fail:
            status = "fail"
        else:
            status = "pass"

        aggregates[tool_id] = {
            "status": status,
            "affected": round((n_fail + n_error) / attempted, 4),
            "versions_affected": sum(1 for v in versions_seen.values() if v),
            "versions_total": len(versions_seen),
            "tests_attempted": attempted,
            "tests_passed": n_success,
            "tests_failed": n_fail,
            "tests_errored": n_error,
            "tests_skipped": n_skip,
        }
    return aggregates


def build_manifest_entry(run_id: str, tests: list[dict], aggregates: dict[str, dict]) -> dict:
    return {
        "run_id": run_id,
        "timestamp": run_timestamp(run_id),
        "duration_seconds": run_duration_seconds(tests),
        "tools_attempted": len(aggregates),
        "tests_total": len(tests),
        "tests_passed": sum(a["tests_passed"] for a in aggregates.values()),
        "tests_failed": sum(a["tests_failed"] for a in aggregates.values()),
        "tests_errored": sum(a["tests_errored"] for a in aggregates.values()),
        "tests_skipped": sum(a["tests_skipped"] for a in aggregates.values()),
        "results_html_url": f"{PAGES_BASE}/tool-tests/{run_id}/results.html",
    }


def build_detail(tests: list[dict]) -> dict:
    """tool_id -> version -> [test entries], for the inspector's per-test
    drill-down. Keeps the raw, un-truncated problem text - signature
    normalization is tabled for a later iteration (v2_raster_plan.md sec 1)."""
    tools: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for t in tests:
        d = t["data"]
        tool_id = d.get("tool_id") or t["id"].rsplit("/", 1)[0]
        version = d.get("tool_version") or ""
        tools[tool_id][version].append(
            {
                "test_index": d.get("test_index"),
                "status": d.get("status"),
                "time_seconds": d.get("time_seconds"),
                "execution_problem": d.get("execution_problem"),
                "output_problems": d.get("output_problems"),
            }
        )
    return {tool_id: dict(versions) for tool_id, versions in tools.items()}


def sync_run_report_to_pages(run_id: str) -> None:
    src = os.path.join(TOOL_TESTS_DIR, run_id, "results.html")
    if not os.path.exists(src):
        return
    dst_dir = os.path.join(DOCS_TOOL_TESTS_DIR, run_id)
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy(src, os.path.join(dst_dir, "results.html"))


def main() -> None:
    run_ids = all_run_dirs()

    manifest = []
    matrix_cells: dict[str, dict[str, dict]] = defaultdict(dict)

    for run_id in run_ids:
        tests = load_run(run_id)
        aggregates = tool_aggregates(tests)

        sync_run_report_to_pages(run_id)
        manifest.append(build_manifest_entry(run_id, tests, aggregates))
        for tool_id, cell in aggregates.items():
            matrix_cells[tool_id][run_id] = cell

        run_dir = os.path.join(RUNS_DIR, run_id)
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "detail.json"), "w") as f:
            json.dump(build_detail(tests), f, separators=(",", ":"))

    os.makedirs(RASTER_DATA_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)

    # Sorted by the *displayed* name (raster.html's own shortName() - the
    # last "/"-segment, matching the row labels users actually read), not
    # the raw tool_id - sorting by full id instead grouped every row by
    # toolshed owner ("repos/iuc/...", "repos/bgruening/...") first, so
    # what looked alphabetic within one owner's cluster reset to another
    # letter at the next owner boundary. Case-insensitive so the handful
    # of capitalized built-in tool names (Filter1, Cut1, ...) interleave
    # with the rest instead of forming their own block up front.
    sorted_tools = sorted(matrix_cells, key=lambda t: (t.rsplit("/", 1)[-1].lower(), t))
    matrix = {
        "tools": sorted_tools,
        "runs": run_ids,
        "cells": {tool_id: matrix_cells[tool_id] for tool_id in sorted_tools},
    }
    with open(MATRIX_PATH, "w") as f:
        json.dump(matrix, f, separators=(",", ":"))

    print(
        f"Wrote {MANIFEST_PATH} ({len(manifest)} runs), {MATRIX_PATH} "
        f"({len(matrix['tools'])} tools x {len(run_ids)} runs), and "
        f"{len(run_ids)} runs/*/detail.json files"
    )


if __name__ == "__main__":
    main()
