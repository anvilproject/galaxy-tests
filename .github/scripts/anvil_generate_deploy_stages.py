"""Regenerate docs/deploy-data/stages.json: per-run ansible-pull task
timing, parsed from every committed
reports/anvil/deployments/<run_id>/ansible-output.log - feeds the
deploy-stage-timing stacked bar chart (docs/deploy-stages.html).

Ansible's `profile_tasks` callback (enabled via
ANSIBLE_CALLBACKS_ENABLED in galaxy-k8s-boot/bin/launch_vm.sh) prints a
timing annotation right after each TASK header, but it reports the
*previous* task's duration, not the upcoming one's - e.g.:

    TASK [Helm install Galaxy] ***
    Thursday ... (0:00:00.031)   0:03:19.594 ***      <- duration of the
    changed: [127.0.0.1]                                 task BEFORE this one

    TASK [Wait for Galaxy PVC to be bound] ***
    Thursday ... (0:06:31.740)   0:09:51.335 ***      <- Helm install
    ok: [127.0.0.1]                                      Galaxy's actual
                                                          391.74s duration

So task[i]'s duration is the parenthetical on the timing line that
follows task[i+1]'s header (confirmed against the log's own end-of-run
profile_tasks summary, which agrees to the millisecond). There is no
paired task for the *last* TASK header via this scheme, but PLAY RECAP
is always followed by one more timing line, which supplies it.

This also does not filter to a recent window, matching the raster data
pipeline: manifest rows are cheap to keep for the full run history, and
the renderer decides how much of it to draw.

Usage: anvil_generate_deploy_stages.py
"""

import json
import os
import re

DEPLOYMENTS_DIR = "reports/anvil/deployments"
OUT_PATH = "docs/deploy-data/stages.json"
GITHUB_BASE = "https://github.com/anvilproject/galaxy-tests/blob/main"

MAX_INDIVIDUAL_STAGES = 6
OTHER_LABEL = "Other tasks"

TASK_RE = re.compile(r"^TASK \[(.+?)\] \*+\s*$")
TIMING_RE = re.compile(r"^\w+ \d+ \w+ \d{4}\s+[\d:]+\s+\+\d{4}\s+\(([\d:.]+)\)\s+[\d:.]+\s+\*+\s*$")
RUN_ID_TIMESTAMP_RE = re.compile(r"-(\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$")


def all_run_ids() -> list[str]:
    if not os.path.isdir(DEPLOYMENTS_DIR):
        return []
    names = sorted(os.listdir(DEPLOYMENTS_DIR))
    return [
        n
        for n in names
        if os.path.isfile(os.path.join(DEPLOYMENTS_DIR, n, "ansible-output.log"))
    ]


def run_timestamp(run_id: str) -> str | None:
    m = RUN_ID_TIMESTAMP_RE.search(run_id)
    if not m:
        return None
    yy, mm, dd, hh, mi, ss = m.groups()
    return f"20{yy}-{mm}-{dd}T{hh}:{mi}:{ss}"


def parse_duration(text: str) -> float:
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def parse_task_durations(log_path: str) -> dict[str, float]:
    """task name -> total seconds (summed if a task name recurs, e.g. a
    role included more than once)."""
    tasks: list[str] = []
    timings: list[float] = []
    with open(log_path, errors="replace") as f:
        for line in f:
            m = TASK_RE.match(line)
            if m:
                tasks.append(m.group(1))
                continue
            m = TIMING_RE.match(line)
            if m:
                timings.append(parse_duration(m.group(1)))

    durations: dict[str, float] = {}
    for i in range(len(tasks) - 1):
        durations[tasks[i]] = durations.get(tasks[i], 0.0) + timings[i + 1]
    return durations


def build_stages(durations: dict[str, float], named_tasks: set[str]) -> list[dict]:
    # named_tasks is the *global* top-N recurring task set (by cumulative
    # duration across every run, computed once in main()) - not this run's
    # own local ranking. Using a global set (rather than each run picking
    # its own top N) is what makes a task's color/stacking position stable
    # across every run: a task that's merely large in one unusual run still
    # lands in "Other tasks" there if it isn't a *generally* large one
    # (v2_startup_ui.md "Keep segment order stable across every run").
    named = [(name, seconds) for name, seconds in durations.items() if name in named_tasks]
    named.sort(key=lambda kv: -kv[1])
    stages = [{"name": name, "seconds": round(seconds, 2)} for name, seconds in named]
    other_seconds = sum(seconds for name, seconds in durations.items() if name not in named_tasks)
    if other_seconds > 0:
        stages.append({"name": OTHER_LABEL, "seconds": round(other_seconds, 2)})
    return stages


def main() -> None:
    run_ids = all_run_ids()
    durations_by_run: dict[str, dict[str, float]] = {}
    for run_id in run_ids:
        log_path = os.path.join(DEPLOYMENTS_DIR, run_id, "ansible-output.log")
        durations = parse_task_durations(log_path)
        if durations:  # e.g. ansible-pull never started - nothing to chart
            durations_by_run[run_id] = durations

    global_totals: dict[str, float] = {}
    for durations in durations_by_run.values():
        for name, seconds in durations.items():
            global_totals[name] = global_totals.get(name, 0.0) + seconds
    named_tasks = {
        name
        for name, _ in sorted(global_totals.items(), key=lambda kv: -kv[1])[:MAX_INDIVIDUAL_STAGES]
    }

    runs = []
    for run_id, durations in durations_by_run.items():
        stages = build_stages(durations, named_tasks)
        runs.append(
            {
                "run_id": run_id,
                "timestamp": run_timestamp(run_id),
                "total_seconds": round(sum(durations.values()), 2),
                "stages": stages,
                "log_url": f"{GITHUB_BASE}/{DEPLOYMENTS_DIR}/{run_id}/ansible-output.log",
            }
        )

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({"runs": runs}, f, separators=(",", ":"))

    print(f"Wrote {OUT_PATH} ({len(runs)} runs)")


if __name__ == "__main__":
    main()
