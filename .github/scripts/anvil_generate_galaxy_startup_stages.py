"""Regenerate docs/deploy-data/galaxy-startup-stages.json: per-run
breakdown of Galaxy's own application startup time, parsed from every
committed reports/anvil/deployments/<run_id>/galaxy-web-startup.log -
feeds the Galaxy-startup-timing stacked bar chart on
docs/deploy-stages.html (below the ansible deploy-stage-timing one).

This is a different axis from that ansible chart: ansible-pull times
provisioning the VM/cluster itself, while this times Galaxy's own
process from "the wait-db init container finished" to "uvicorn served
its first request" - i.e. how long Galaxy takes to become usable once
the cluster is already up.

There's no single log line that announces "entering phase X" the way
ansible-pull's TASK headers do. Instead this walks the log
chronologically looking for the first line matching each phase
boundary's marker, in a fixed order confirmed against one real,
uncorrupted capture (see galaxy-tool-tests run 32435916879):

  1. Waiting for database   - the galaxy-wait-db init container's own
                               "Initialization waits starting/complete"
                               lines (a different container/log format
                               entirely).
  2. App bootstrap           - process start (imports, config load, DB
                               migration check) up to the first
                               timestamped application log line. Most
                               of this phase's own lines predate
                               Galaxy's logging setup and carry no
                               timestamp at all, so it can't be broken
                               down further.
  3. Datatypes & job config  - datatype registry, job_conf, queue
                               worker init, up to the first tool being
                               parsed.
  4. Tool & datatype loading  - by far the largest phase in the one run
                               inspected so far (~28k of ~32k lines):
                               parsing every tool_conf/shed_tool_conf
                               entry, one XML file at a time.
  5. App finalization        - visualization plugins, workflow
                               scheduling, web framework controller
                               registration, once the toolbox itself is
                               built.
  6. Workers ready            - WSGI/ASGI app construction and worker
                               postfork setup, up to the first request
                               uvicorn actually serves.

A marker that never appears (e.g. a future Galaxy version without
some subsystem, or a log that's missing the wait-db container
entirely) just means that boundary isn't detected - its would-be
duration folds into the next phase that IS found, rather than
crashing. This has only been checked against a single clean run, so
treat the phase boundaries as a first cut to refine once more runs
land.

Usage: anvil_generate_galaxy_startup_stages.py
"""

import json
import os
import re
from datetime import datetime

DEPLOYMENTS_DIR = "reports/anvil/deployments"
OUT_PATH = "docs/deploy-data/galaxy-startup-stages.json"
GITHUB_BASE = "https://github.com/anvilproject/galaxy-tests/blob/main"
LOG_FILENAME = "galaxy-web-startup.log"

RUN_ID_TIMESTAMP_RE = re.compile(r"-(\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$")

# "[pod/galaxy-web-68494fd697-t8jjh/galaxy-web] ..." - container name is
# the last path segment before the closing bracket.
PREFIX_RE = re.compile(r"^\[pod/[^/\]]+/([^\]]+)\]\s?(.*)$")

# Standard python-logging line, once Galaxy's own logging is configured:
# "<logger> <LEVEL> YYYY-MM-DD HH:MM:SS,mmm [...] message"
APP_LOG_RE = re.compile(r"^(\S+)\s+(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})\b")

# The wait-db init container prints plain `date`-style lines instead:
# "[Fri Aug 21 01:28:33 AM UTC 2026] Initialization waits starting."
WAIT_DB_TS_RE = re.compile(r"^\[(\w+ \w+ +\d+ \d+:\d+:\d+ [AP]M UTC \d+)\]\s*(.*)$")

PHASE_ORDER = [
    "Waiting for database",
    "App bootstrap",
    "Datatypes & job config",
    "Tool & datatype loading",
    "App finalization",
    "Workers ready",
]


def all_run_ids() -> list[str]:
    if not os.path.isdir(DEPLOYMENTS_DIR):
        return []
    names = sorted(os.listdir(DEPLOYMENTS_DIR))
    return [n for n in names if os.path.isfile(os.path.join(DEPLOYMENTS_DIR, n, LOG_FILENAME))]


def run_timestamp(run_id: str) -> str | None:
    m = RUN_ID_TIMESTAMP_RE.search(run_id)
    if not m:
        return None
    yy, mm, dd, hh, mi, ss = m.groups()
    return f"20{yy}-{mm}-{dd}T{hh}:{mi}:{ss}"


def parse_events(log_path: str) -> list[tuple[datetime, str, str | None, str]]:
    """(timestamp, container, logger-or-None, message) for every line
    with a parseable timestamp, in file order. Lines without one
    (mostly pre-logging-setup import/warning noise) are dropped - they
    can't anchor a phase boundary and their small number of seconds is
    absorbed into whichever phase is open at the time."""
    events = []
    with open(log_path, errors="replace") as f:
        for line in f:
            m = PREFIX_RE.match(line)
            if not m:
                continue
            container, rest = m.groups()

            wm = WAIT_DB_TS_RE.match(rest)
            if wm:
                try:
                    ts = datetime.strptime(wm.group(1), "%a %b %d %I:%M:%S %p UTC %Y")
                except ValueError:
                    continue
                events.append((ts, container, None, wm.group(2)))
                continue

            am = APP_LOG_RE.match(rest)
            if am:
                logger, ts_str, ms = am.groups()
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(microsecond=int(ms) * 1000)
                events.append((ts, container, logger, rest))

    events.sort(key=lambda e: e[0])
    return events


def find_first(events, start_idx: int, predicate) -> int | None:
    for i in range(start_idx, len(events)):
        if predicate(events[i]):
            return i
    return None


def build_phase_seconds(events: list[tuple[datetime, str, str | None, str]]) -> dict[str, float]:
    if not events:
        return {}

    def is_wait_db_start(e):
        return e[1] == "galaxy-wait-db" and "Initialization waits starting." in e[3]

    def is_wait_db_complete(e):
        return e[1] == "galaxy-wait-db" and "Initialization waits complete." in e[3]

    def is_first_app_line(e):
        return e[1] == "galaxy-web" and e[2] is not None

    def is_toolbox_start(e):
        return e[2] is not None and e[2].startswith("galaxy.tool_util.toolbox")

    def is_finalization_start(e):
        return e[2] in ("galaxy.tools.special_tools", "galaxy.visualization.plugins.registry")

    def is_webapps_start(e):
        return e[2] is not None and e[2].startswith("galaxy.webapps")

    def is_first_uvicorn(e):
        return e[2] == "uvicorn.access"

    boundaries = [
        is_wait_db_complete,
        is_first_app_line,
        is_toolbox_start,
        is_finalization_start,
        is_webapps_start,
        is_first_uvicorn,
    ]

    start_idx = find_first(events, 0, is_wait_db_start)
    cursor = start_idx if start_idx is not None else 0
    phase_start = events[cursor][0]
    seconds: dict[str, float] = {}

    for phase_idx, boundary in enumerate(boundaries):
        found = find_first(events, cursor, boundary)
        if found is None:
            continue  # marker absent - its slice folds into the next phase found
        phase_end = events[found][0]
        delta = (phase_end - phase_start).total_seconds()
        if delta > 0:
            name = PHASE_ORDER[phase_idx]
            seconds[name] = seconds.get(name, 0.0) + delta
        phase_start = phase_end
        cursor = found

    return seconds


def main() -> None:
    run_ids = all_run_ids()
    runs = []
    for run_id in run_ids:
        log_path = os.path.join(DEPLOYMENTS_DIR, run_id, LOG_FILENAME)
        events = parse_events(log_path)
        seconds = build_phase_seconds(events)
        if not seconds:
            continue  # e.g. the log fetch failed or never captured a full sequence
        stages = [
            {"name": name, "seconds": round(seconds[name], 2)}
            for name in PHASE_ORDER
            if name in seconds
        ]
        runs.append(
            {
                "run_id": run_id,
                "timestamp": run_timestamp(run_id),
                "total_seconds": round(sum(seconds.values()), 2),
                "stages": stages,
                "log_url": f"{GITHUB_BASE}/{DEPLOYMENTS_DIR}/{run_id}/{LOG_FILENAME}",
            }
        )

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({"runs": runs}, f, separators=(",", ":"))

    print(f"Wrote {OUT_PATH} ({len(runs)} runs)")


if __name__ == "__main__":
    main()
