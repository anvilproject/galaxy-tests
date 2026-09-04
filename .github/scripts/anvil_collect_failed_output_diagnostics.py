"""Collect, for every failed/errored test's output datasets, three numbers
that only exist while the VM is still up: the database's own file_size,
the actual byte count on disk, and the byte count served back through
Galaxy's internal nginx - writing the result to
reports/anvil/deployments/<run_id>/failed-output-diagnostics.tsv.

Why (outstanding.md #8, "The VM is deleted at run end"): the empty-output
investigation (#1) needed a live instance twice and didn't have one -
this run only produced findings because someone happened to keep the VM
up and sample it by hand. This script is the standing version of that
one-off sampling, run automatically before every teardown instead of on
request. It does not diagnose anything itself - it only captures the
three numbers a human would need to start (do the DB and disk agree? does
what nginx serves match either of them?), so a future investigation
starts with evidence already in hand instead of an already-deleted VM.

Deliberately scoped to failed/error tests only, not every dataset in the
run (see BAD_STATUSES) - the question this answers is "why did this
*fail*", not a full audit of every output.

Requires kubectl already configured against the live cluster (see
"Copy kubeconfig from VM") and a running kubectl port-forward-free path:
this script opens its own short-lived port-forward to galaxy-nginx to
fetch the "served through nginx" figure, matching the internal-cluster
route already established (not the external ingress path the test
client itself uses - that's a separate, deliberate comparison, not this
script's job).

Usage: anvil_collect_failed_output_diagnostics.py <results.json path> <out.tsv path>
Env: GALAXY_URL, KEY (Galaxy API key) - used only to resolve each
     dataset's history_id/hda_id into the nginx-internal request path;
     no external network calls are made.
"""

import json
import os
import re
import subprocess
import sys
import time

BAD_STATUSES = {"error", "failure"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
PORT_FORWARD_LOCAL_PORT = 18765
REQUEST_TIMEOUT = 10  # per-request, internal-cluster hop - should normally take well under 1s


def collect_failed_outputs(results_path: str) -> list[dict]:
    with open(results_path) as f:
        data = json.load(f)

    rows = []
    seen_uuids = set()
    for t in data.get("tests", []):
        d = t.get("data", {})
        if d.get("status") not in BAD_STATUSES:
            continue
        job = d.get("job") or {}
        history_id = job.get("history_id")
        for output_name, out in (job.get("outputs") or {}).items():
            uuid = out.get("uuid")
            hda_id = out.get("id")
            if not uuid or not hda_id or not UUID_RE.match(uuid):
                continue
            if uuid in seen_uuids:
                continue
            seen_uuids.add(uuid)
            rows.append(
                {
                    "test_id": t.get("id"),
                    "tool_id": d.get("tool_id"),
                    "output_name": output_name,
                    "history_id": history_id,
                    "hda_id": hda_id,
                    "uuid": uuid,
                }
            )
    return rows


def fetch_db_file_info(uuids: list[str]) -> dict[str, dict]:
    """uuid -> {file_name, file_size} via a single batched psql query -
    HDA/dataset UUIDs are real DB columns, unlike Galaxy's signed/encoded
    API ids, so no id-decoding is needed here."""
    if not uuids:
        return {}
    array_literal = "ARRAY[" + ",".join(f"'{u}'" for u in uuids) + "]::uuid[]"
    sql = (
        "SELECT hda.uuid, d.file_name, d.file_size "
        "FROM history_dataset_association hda "
        "JOIN dataset d ON hda.dataset_id = d.id "
        f"WHERE hda.uuid = ANY({array_literal});"
    )
    result = subprocess.run(
        ["kubectl", "exec", "-n", "galaxy", "galaxy-postgres-1", "--", "psql", "-U", "postgres", "-d", "galaxy", "-A", "-t", "-F", "\t", "-c", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"DB query failed: {result.stderr}", file=sys.stderr)
        return {}

    info = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        uuid, file_name, file_size = parts
        info[uuid] = {"file_name": file_name or None, "file_size": int(file_size) if file_size else None}
    return info


def fetch_disk_bytes(file_names: list[str]) -> dict[str, int | None]:
    """file_name -> actual byte count on disk, via one batched kubectl
    exec (a stat per file over separate exec calls would be far slower)."""
    file_names = [f for f in file_names if f]
    if not file_names:
        return {}
    script = "\n".join(f'stat -c "%s %n" {json.dumps(f)} 2>/dev/null || echo "MISSING {f}"' for f in file_names)
    result = subprocess.run(
        ["kubectl", "exec", "-i", "-n", "galaxy", "deployment/galaxy-job-0", "--", "sh", "-s"],
        input=script,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"disk stat batch failed: {result.stderr}", file=sys.stderr)

    sizes: dict[str, int | None] = {}
    for line in result.stdout.strip().splitlines():
        if line.startswith("MISSING "):
            sizes[line[len("MISSING ") :]] = None
            continue
        size_str, _, name = line.partition(" ")
        try:
            sizes[name] = int(size_str)
        except ValueError:
            continue
    return sizes


def start_port_forward() -> subprocess.Popen:
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", "galaxy", "svc/galaxy-nginx", f"{PORT_FORWARD_LOCAL_PORT}:8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(0.5)
        probe = subprocess.run(
            ["curl", "-sf", "-o", "/dev/null", f"http://localhost:{PORT_FORWARD_LOCAL_PORT}/galaxy/api/version"],
        )
        if probe.returncode == 0:
            break
    return proc


def fetch_nginx_bytes(row: dict, key: str) -> int | None:
    url = (
        f"http://localhost:{PORT_FORWARD_LOCAL_PORT}/galaxy/api/histories/"
        f"{row['history_id']}/contents/{row['hda_id']}/display?raw=true"
    )
    try:
        result = subprocess.run(
            ["curl", "-sf", "-H", f"x-api-key: {key}", "-o", "/dev/null", "-w", "%{size_download}", url],
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        return None


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: anvil_collect_failed_output_diagnostics.py <results.json> <out.tsv>")
    results_path, out_path = sys.argv[1], sys.argv[2]
    key = os.environ["KEY"]

    rows = collect_failed_outputs(results_path)
    print(f"{len(rows)} distinct failed-test output datasets to diagnose")
    if not rows:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            f.write("test_id\ttool_id\toutput_name\thistory_id\thda_id\tuuid\tdb_file_size\tdisk_bytes\tnginx_bytes\n")
        return

    db_info = fetch_db_file_info([r["uuid"] for r in rows])
    file_names = [db_info[r["uuid"]]["file_name"] for r in rows if r["uuid"] in db_info]
    disk_bytes = fetch_disk_bytes(file_names)

    pf = start_port_forward()
    try:
        for i, r in enumerate(rows):
            r["nginx_bytes"] = fetch_nginx_bytes(r, key)
            if (i + 1) % 100 == 0:
                print(f"  fetched {i + 1}/{len(rows)}")
    finally:
        pf.terminate()

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write("test_id\ttool_id\toutput_name\thistory_id\thda_id\tuuid\tdb_file_size\tdisk_bytes\tnginx_bytes\n")
        for r in rows:
            info = db_info.get(r["uuid"], {})
            file_name = info.get("file_name")
            f.write(
                "\t".join(
                    str(v) if v is not None else ""
                    for v in [
                        r["test_id"],
                        r["tool_id"],
                        r["output_name"],
                        r["history_id"],
                        r["hda_id"],
                        r["uuid"],
                        info.get("file_size"),
                        disk_bytes.get(file_name) if file_name else None,
                        r.get("nginx_bytes"),
                    ]
                )
                + "\n"
            )

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
