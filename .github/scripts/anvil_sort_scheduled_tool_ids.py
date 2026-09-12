"""Reorder .github/scheduled-tool-ids.txt longest-running tools first.

Measured durations when recent runs provide them, GCP Batch routing as the
proxy when they do not.

Why order at all: the scheduled run's dispatch loop (anvil-test.yaml's "Run tool tests"
step) reads this file top to bottom, forking one worker per line as client
slots free up. Local-routed tools are individually fast and cycle through
quickly; GCP Batch jobs are individually slow (each provisions its own VM
and can run for many minutes). Mixed randomly through one shared worker
pool, GCP Batch's share of active slots only builds up gradually, over a
timescale comparable to its own job duration - confirmed empirically
against real run data (docs/parallelism-data): GCP Batch concurrency took
~30 minutes to ramp to its peak while local concurrency saturated and
drained within the first few minutes. Front-loading Batch-routed tools
lets their long individual runtimes start consuming wall-clock time from
minute 0 instead of trickling in - shrinking both the ramp-up and,
likely, total run duration (bounded by whenever the last Batch job
finishes).

Classification mirrors the actual TPV destination-selection rule deployed
by galaxy-k8s-boot's values/values.yml (jobs.rules.tpv_rules_local.yml):
the k8s (local) destination only accepts jobs with cores <= 1 and
mem <= 4 (GB); anything over either limit falls through to gcp_batch,
which has no cap. Per-tool cores/mem come from the shared community TPV
database (tpv-shared-database's tools.yml - see
https://github.com/galaxyproject/tpv-shared-database), falling back to
its own `default` rule (cores=1, mem=cores*3.8) for tools with no
specific entry. A handful of internal Galaxy utility tools/patterns are
force-routed to `local` regardless of resources (values/values.yml's
`tools:` id-override list) - matched here too, so they don't get
misclassified as gcp_batch.

This is a static, point-in-time classification, not live TPV evaluation:
it doesn't execute tools.yml's conditional `rules:` (e.g. per-context
resource adjustments), just the plain cores/mem fields - a reasonable
approximation for ordering purposes, not a guarantee of exactly matching
what TPV will actually pick for every tool. It also goes stale as tool
versions/resource requirements/the shared DB change - re-run periodically
and whenever scheduled-tool-ids.txt itself is regenerated (see CLAUDE.md).

Usage: anvil_sort_scheduled_tool_ids.py <path to tpv-shared-database>/tools.yml
       (writes .github/scheduled-tool-ids.txt in place)
"""

import collections
import glob
import json
import re
import sys

import yaml

SCHEDULED_LIST_PATH = ".github/scheduled-tool-ids.txt"
RESULTS_GLOB = "reports/anvil/tool-tests/*/results.json"
# Enough runs to smooth over a tool that failed fast once; few enough to
# still reflect the current pinned revisions.
RUNS_TO_CONSIDER = 3
# A tool's own test cases run inside one worker at this width, so its
# contribution to the run is its cases packed into that many lanes, not
# their sum. Keep in step with --parallel-tests in anvil-test.yaml.
INNER_PARALLELISM = 4

# k8s (local) destination's caps - values/values.yml's tpv_rules_local.yml
# `destinations.k8s`. Anything exceeding either falls through to gcp_batch,
# which has no cap of its own (per-job right-sized GCE VM).
K8S_MAX_CORES = 1
K8S_MAX_MEM = 4

# Tools/patterns force-routed to `local` regardless of resource footprint -
# values/values.yml's `jobs.rules.tpv_rules_local.yml.tools` id-override
# list. Real toolshed tool IDs (what scheduled-tool-ids.txt actually
# contains) rarely match these, but check anyway for correctness.
LOCAL_OVERRIDE_PATTERNS = [
    re.compile(p)
    for p in [
        r"^upload1$",
        r"^__DATA_FETCH__$",
        r"^__EXPORT_HISTORY__$",
        r"^__IMPORT_HISTORY__$",
        r"^__SET_METADATA__$",
        r"^__EXTRACT_DATASET__$",
        r"^interactive_tool.*",
        r".*data_source.*",
        r"^__EXPORT_WORKFLOW__$",
        r"^__IMPORT_WORKFLOW__$",
        r"^random_lines1$",
        r"^compose_text_param$",
        r"^map_param_value$",
        r"^param_value_from_file$",
        r"^tp_awk_tool$",
        r"^tp_grep_tool$",
    ]
]


def load_tool_rules(tools_yml_path: str) -> tuple[dict, dict]:
    with open(tools_yml_path) as f:
        db = yaml.safe_load(f)
    default = db["tools"]["default"]
    rules = {}
    for key, rule in db["tools"].items():
        if key == "default":
            continue
        try:
            rules[re.compile(key)] = rule
        except re.error:
            continue  # a handful of keys aren't valid regex as-is; skip rather than guess
    return rules, default


def resolve_expr(value, cores: float, fallback: float) -> float:
    """tools.yml numeric fields are sometimes plain numbers and sometimes
    small Python expressions (f-string style) referencing `cores` or - for
    a handful of entries - dynamic values like `input_size` that aren't
    known statically here. Best-effort: evaluate what we can; anything
    that references something we don't have falls back to a static
    default rather than guessing, biasing classification toward whatever
    the tool's other resolved dimension already suggests."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(eval(str(value), {}, {"cores": cores}))
    except Exception:
        return fallback


def resolve_cores_mem(tool_id: str, rules: dict, default: dict) -> tuple[float, float]:
    matched = None
    for pattern, rule in rules.items():
        if pattern.match(tool_id):
            matched = rule
            break
    default_cores = float(default["cores"])
    cores = resolve_expr((matched or {}).get("cores", default_cores), default_cores, default_cores)
    default_mem = cores * 3.8
    mem = resolve_expr((matched or {}).get("mem", default_mem), cores, default_mem)
    return cores, mem


def classify(tool_id: str, rules: dict, default: dict) -> str:
    if any(p.match(tool_id) for p in LOCAL_OVERRIDE_PATTERNS):
        return "local"
    cores, mem = resolve_cores_mem(tool_id, rules, default)
    return "local" if (cores <= K8S_MAX_CORES and mem <= K8S_MAX_MEM) else "gcp_batch"


def unversioned(tool_id: str) -> str:
    """Strip the trailing version from a toolshed tool id.

    The scheduled list carries versions; results.json records the same tools
    without one, and built-in tools such as `Count1` have neither. Both sides
    have to be reduced to the same shape or nothing joins.
    """
    parts = tool_id.split("/")
    # owner/repo/tool plus the toolshed host and "repos" is five; a sixth is
    # the version.
    return "/".join(parts[:-1]) if len(parts) >= 6 else tool_id


def tool_wall_seconds(case_seconds: list, lanes: int = INNER_PARALLELISM) -> float:
    """Longest lane when a tool's cases are packed into its worker's threads."""
    packed = [0.0] * lanes
    for seconds in sorted(case_seconds, reverse=True):
        packed[packed.index(min(packed))] += seconds
    return max(packed)


def measured_durations() -> dict:
    """Per-tool wall time from the most recent runs, longest seen per tool.

    Longest rather than mean: scheduling wants to know how long a tool can
    hold a slot, and a tool that failed fast on one run still costs its full
    time on the next.
    """
    durations: dict = {}
    for path in sorted(glob.glob(RESULTS_GLOB))[-RUNS_TO_CONSIDER:]:
        try:
            with open(path) as f:
                tests = json.load(f).get("tests", [])
        except (OSError, ValueError):
            continue
        per_tool: dict = collections.defaultdict(list)
        for test in tests:
            data = test.get("data", {})
            seconds = data.get("time_seconds")
            if data.get("status") != "skip" and isinstance(seconds, (int, float)):
                per_tool[data.get("tool_id")].append(seconds)
        for tool_id, case_seconds in per_tool.items():
            key = unversioned(tool_id)
            wall = tool_wall_seconds(case_seconds)
            if wall > durations.get(key, 0.0):
                durations[key] = wall
    return durations


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    rules, default = load_tool_rules(sys.argv[1])

    with open(SCHEDULED_LIST_PATH) as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]

    durations = measured_durations()

    def sort_key(tool_id: str):
        measured = durations.get(unversioned(tool_id))
        if measured is not None:
            return (0, -measured)
        # No timing yet: fall back to routing, which is a coarse proxy for
        # the same thing, and place these after the tools we can rank.
        return (1, 0 if classify(tool_id, rules, default) == "gcp_batch" else 1)

    ordered = sorted(lines, key=sort_key)

    # This file selects which revision of each tool is tested, so the entries
    # must survive verbatim - the unversioned form exists only to join against
    # measured durations, and must never reach the file.
    assert sorted(ordered) == sorted(lines), "sorting must reorder the list, not rewrite its entries"

    with open(SCHEDULED_LIST_PATH, "w") as f:
        f.write("\n".join(ordered) + "\n")

    timed = sum(1 for line in lines if unversioned(line) in durations)
    print(
        f"Sorted {len(lines)} tool IDs longest-first: {timed} by measured duration "
        f"from the last {RUNS_TO_CONSIDER} runs, {len(lines) - timed} by GCP Batch routing"
    )
    # Most scheduled tools have run recently, so a low match rate means the two
    # sides stopped agreeing on how a tool is named rather than that the runs
    # were short of data - which silently degrades this back to a routing sort.
    if lines and timed < len(lines) // 2:
        print(
            f"WARNING: only {timed} of {len(lines)} scheduled tools matched a measured "
            "duration. Check that results.json tool IDs still line up with the "
            "scheduled list; ordering has fallen back to routing for the rest."
        )


if __name__ == "__main__":
    main()
