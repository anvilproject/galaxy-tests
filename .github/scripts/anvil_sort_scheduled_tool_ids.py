"""Reorder .github/scheduled-tool-ids.txt so tools that route to GCP Batch
come first, local/k8s-routed tools after.

Why: the scheduled run's dispatch loop (anvil-test.yaml's "Run tool tests"
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

import re
import sys

import yaml

SCHEDULED_LIST_PATH = ".github/scheduled-tool-ids.txt"

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


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    rules, default = load_tool_rules(sys.argv[1])

    with open(SCHEDULED_LIST_PATH) as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]

    batch, local = [], []
    for line in lines:
        (batch if classify(line, rules, default) == "gcp_batch" else local).append(line)

    with open(SCHEDULED_LIST_PATH, "w") as f:
        f.write("\n".join(batch + local) + "\n")

    print(f"Sorted {len(lines)} tool IDs: {len(batch)} gcp_batch-classified first, {len(local)} local/k8s after")


if __name__ == "__main__":
    main()
