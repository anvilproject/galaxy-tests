#!/usr/bin/env python3
"""Run a matrix of single-tool jobs against a Galaxy instance and record metrics.

The matrix is one or more YAML files (see benchmarks/tools/). Each entry names
a tool and its inputs; inputs may be literals, datasets or collections looked
up by name in a staged history, or outputs of other entries. Entries form a
DAG, so a matrix can build its own BAMs and VCFs from the staged reads and
then fan out every downstream tool over them.

    anvil_tool_matrix.py run MATRIX.yml [MATRIX.yml ...] --replicas K \
        --output-dir DIR [--history-prefix NAME] [--run-label LABEL]

runs the whole matrix K times concurrently, one history per replica, so K is
the number of simultaneous users the instance is asked to serve. Every job's
API record and job metrics are written to DIR/<job id>.json in the shape abm
uses (`metrics` holds the job record with `job_metrics`), which
anvil_benchmark_summary.py reads, and DIR/matrix-run.json records the
submission timeline.

    anvil_tool_matrix.py validate MATRIX.yml ... [--schema-dir DIR]

checks that every tool resolves and every input key, select value and dataset
reference is well formed, against the live server or a directory of cached
`/api/tools/<id>?io_details=true` documents.

    anvil_tool_matrix.py describe TOOL [--schema-dir DIR]

prints the flattened input keys of a tool, the form the matrix uses.

Connection: --url/--key, else GALAXY_URL/GALAXY_API_KEY, else the abm profile
named by --profile in ~/.abm/profile.yml or .abm/profile.yml.

Matrix file format:

    history: Variant 2GB inputs          # default history for name lookups
    tools:
      - id: bwa                          # unique across all files; default: tool
        tool: bwa_mem                    # short name, owner/name or full tool id
        history: Variant 2GB inputs      # per-entry override
        inputs:
          reference_source|reference_source_selector: cached
          reference_source|ref_file: hg38
          fastq_input|fastq_input1: {collection: SRR24043307-2GB}
      - tool: samtools_stats
        inputs:
          input: {from: bwa, output: bam_output}

Input value forms:
  literal                       passed as is
  {dataset: NAME}               HDA named NAME in the entry's history
  {collection: NAME}            HDCA named NAME in the entry's history
  {from: ENTRY, output: NAME}   output dataset NAME of another entry (NAME
                                may be omitted when the tool has one output)
  {from: ENTRY, collection: NAME}  output collection NAME of another entry
  {batch: <any dataset/collection form>}  map the tool over the collection
  [ref, ref, ...]               several datasets for a multiple="true" input
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import yaml

try:
    from bioblend.galaxy import GalaxyInstance
except ImportError:  # validate/describe against --schema-dir do not need it
    GalaxyInstance = None  # type: ignore


# --------------------------------------------------------------------------- #
# Matrix loading
# --------------------------------------------------------------------------- #

class MatrixError(Exception):
    pass


def load_matrix(paths: list[str]) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        doc = yaml.safe_load(open(path)) or {}
        default_history = doc.get("history")
        for raw in doc.get("tools") or []:
            if "tool" not in raw:
                raise MatrixError(f"{path}: entry without 'tool': {raw}")
            entry = dict(raw)
            entry.setdefault("id", entry["tool"])
            entry.setdefault("history", default_history)
            entry.setdefault("inputs", {})
            entry["_file"] = path
            if entry["id"] in seen:
                raise MatrixError(f"{path}: duplicate entry id {entry['id']!r}")
            seen.add(entry["id"])
            entries.append(entry)
    ids = {e["id"] for e in entries}
    for e in entries:
        e["_deps"] = sorted({ref["from"] for ref in iter_refs(e["inputs"]) if "from" in ref})
        for dep in e["_deps"]:
            if dep not in ids:
                raise MatrixError(f"{e['_file']}: entry {e['id']!r} depends on unknown entry {dep!r}")
    order = topo_order(entries)
    return [next(e for e in entries if e["id"] == i) for i in order]


def iter_refs(value):
    """Yield every dataset/collection/from reference dict inside an inputs tree."""
    if isinstance(value, dict):
        if is_ref(value):
            yield value
            if "batch" in value and isinstance(value["batch"], dict):
                yield from iter_refs(value["batch"])
        else:
            for v in value.values():
                yield from iter_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_refs(v)


def is_ref(value) -> bool:
    return isinstance(value, dict) and bool({"dataset", "collection", "from", "batch"} & set(value))


def topo_order(entries: list[dict]) -> list[str]:
    deps = {e["id"]: set(e["_deps"]) for e in entries}
    order: list[str] = []
    while deps:
        ready = sorted(i for i, d in deps.items() if not d)
        if not ready:
            raise MatrixError(f"dependency cycle among {sorted(deps)}")
        order.extend(ready)
        for i in ready:
            deps.pop(i)
        for d in deps.values():
            d.difference_update(ready)
    return order


# --------------------------------------------------------------------------- #
# Tool schema handling (shared by validate/describe and run)
# --------------------------------------------------------------------------- #

def short_tool_name(tool_id: str) -> str:
    parts = tool_id.split("/")
    return parts[4] if tool_id.startswith("toolshed") and len(parts) >= 6 else parts[0]


def version_key(tool_id: str):
    if not tool_id.startswith("toolshed"):
        return [0]
    version = tool_id.rsplit("/", 1)[-1]
    return [int(x) if x.isdigit() else x for x in re.split(r"[.+]", version.replace("galaxy", ""))]


def resolve_tool_id(spec: str, all_ids: list[str]) -> str:
    """Resolve a short name, owner/name or full id to the newest installed id."""
    if spec in all_ids:
        return spec
    if "/" in spec and not spec.startswith("toolshed"):
        owner, name = spec.split("/", 1)
        cands = [t for t in all_ids if t.startswith("toolshed") and t.split("/")[2] == owner
                 and short_tool_name(t) == name]
    else:
        cands = [t for t in all_ids if short_tool_name(t) == spec]
    if not cands:
        raise MatrixError(f"tool {spec!r} is not installed")
    # Prefer the newest version; ignore mixed-type comparison noise.
    try:
        return max(cands, key=version_key)
    except TypeError:
        return sorted(cands)[-1]


def flatten_schema(inputs: list[dict], prefix: str = "", flat: dict[str, list[dict]] | None = None) -> dict[str, list[dict]]:
    """Map pipe-joined input keys to their parameter definitions.

    Repeats are keyed with a `_N` placeholder (`name_N|param`); conditionals
    expose the test parameter and every case's inputs under the same prefix.
    A key that appears in several cases (often with different types, such as
    a cached-index select versus a history dataset) keeps every alternative.
    """
    if flat is None:
        flat = {}
    for p in inputs:
        name = p.get("name")
        key = f"{prefix}{name}"
        ptype = p.get("type")
        if ptype == "conditional":
            test = p.get("test_param") or {}
            flat.setdefault(f"{key}|{test.get('name')}", []).append(test)
            for case in p.get("cases") or []:
                flatten_schema(case.get("inputs") or [], f"{key}|", flat)
        elif ptype == "repeat":
            flatten_schema(p.get("inputs") or [], f"{key}_N|", flat)
        elif ptype == "section":
            flatten_schema(p.get("inputs") or [], f"{key}|", flat)
        else:
            flat.setdefault(key, []).append(p)
    return flat


def normalize_key(key: str, flat: dict[str, dict]) -> str | None:
    """Return the schema key matching a user key, treating repeat indices as _N."""
    if key in flat:
        return key
    generic = re.sub(r"_\d+(\||$)", r"_N\1", key)
    return generic if generic in flat else None


def check_value(key: str, value, p: dict) -> str | None:
    """Return a problem string if value does not fit parameter definition p."""
    ptype = p.get("type")
    if ptype in ("data", "data_collection"):
        refs = value if isinstance(value, list) else [value]
        for ref in refs:
            if not is_ref(ref):
                return f"{key}: expected a dataset/collection reference, got {ref!r}"
        if ptype == "data_collection" and not any(
                "collection" in r or "batch" in r for r in refs if isinstance(r, dict)):
            return f"{key}: data_collection input needs a collection reference"
        if isinstance(value, list) and not p.get("multiple"):
            return f"{key}: list given but input is not multiple"
        return None
    if is_ref(value):
        return f"{key}: dataset reference given for a {ptype} parameter"
    if ptype == "select" and not p.get("is_dynamic") and p.get("options"):
        allowed = {str(o[1]) for o in p["options"] if isinstance(o, (list, tuple)) and len(o) > 1}
        values = value if isinstance(value, list) else [value]
        for v in values:
            if allowed and str(v) not in allowed:
                return f"{key}: {v!r} not in {sorted(allowed)}"
    elif ptype == "boolean" and not isinstance(value, bool):
        if str(value).lower() not in ("true", "false"):
            return f"{key}: boolean expected, got {value!r}"
    elif ptype == "integer" and value not in (None, ""):
        try:
            int(value)
        except (TypeError, ValueError):
            return f"{key}: integer expected, got {value!r}"
    return None


def validate_entry(entry: dict, schema: dict, tool_id: str) -> list[str]:
    problems: list[str] = []
    flat = flatten_schema(schema.get("inputs") or [])
    for key, value in entry["inputs"].items():
        skey = normalize_key(key, flat)
        if skey is None:
            problems.append(f"unknown input {key!r} (valid: {', '.join(sorted(flat)[:12])}{' ...' if len(flat) > 12 else ''})")
            continue
        results = [check_value(key, value, p) for p in flat[skey]]
        if all(results):  # every alternative rejected the value
            problems.append(results[0])
    return problems


def output_names(schema: dict) -> set[str]:
    return {o["name"] for o in schema.get("outputs") or [] if o.get("name")}


def validate_refs(entry: dict, resolved: dict[str, str], source: "SchemaSource") -> list[str]:
    """Check that every {from: X, output/collection: Y} names an output X has."""
    problems = []
    for ref in iter_refs(entry["inputs"]):
        if "from" not in ref:
            continue
        dep_tid = resolved.get(ref["from"])
        if dep_tid is None:
            continue
        names = output_names(source.schema(dep_tid))
        want = ref.get("output", ref.get("collection"))
        if want is None:
            if len(names) != 1:
                problems.append(f"{ref['from']}: has outputs {sorted(names)}; name one with output: or collection:")
        elif names and want not in names:
            problems.append(f"{ref['from']} has no output {want!r}; outputs are {sorted(names)}")
    return problems


class SchemaSource:
    """Tool ids and io_details from a live server or a cache directory."""

    def __init__(self, gi=None, schema_dir: str | None = None):
        self.gi = gi
        self.schema_dir = schema_dir
        self._ids: list[str] | None = None
        self._cache: dict[str, dict] = {}
        if schema_dir:
            for path in glob.glob(os.path.join(schema_dir, "*.json")):
                try:
                    doc = json.load(open(path))
                except ValueError:
                    continue
                if isinstance(doc, dict) and "id" in doc and "inputs" in doc:
                    self._cache[doc["id"]] = doc

    def all_ids(self) -> list[str]:
        if self._ids is None:
            if self.gi is not None:
                self._ids = [t["id"] for t in self.gi.tools.get_tools()]
            else:
                self._ids = sorted(self._cache)
        return self._ids

    def schema(self, tool_id: str) -> dict:
        if tool_id not in self._cache:
            if self.gi is None:
                raise MatrixError(f"no cached schema for {tool_id}")
            self._cache[tool_id] = self.gi.tools.show_tool(tool_id, io_details=True)
        return self._cache[tool_id]


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

def abm_profile(name: str) -> tuple[str | None, str | None]:
    for path in (os.path.join(".abm", "profile.yml"), os.path.expanduser("~/.abm/profile.yml")):
        if os.path.exists(path):
            prof = (yaml.safe_load(open(path)) or {}).get(name) or {}
            if prof.get("url") and prof.get("key"):
                return prof["url"], prof["key"]
    return None, None


def connect(args) -> "GalaxyInstance":
    if GalaxyInstance is None:
        raise MatrixError("bioblend is not installed")
    url = args.url or os.environ.get("GALAXY_URL")
    key = args.key or os.environ.get("GALAXY_API_KEY")
    if not (url and key):
        purl, pkey = abm_profile(args.profile)
        url, key = url or purl, key or pkey
    if not (url and key):
        raise MatrixError("no Galaxy URL/API key: use --url/--key, GALAXY_URL/GALAXY_API_KEY or an abm profile")
    gi = GalaxyInstance(url=url, key=key)
    gi.max_get_attempts = 5
    gi.get_retry_delay = 5
    return gi


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Runner:
    TERMINAL_BAD = {"error", "deleted", "deleted_new", "failed", "paused", "stopped"}

    def __init__(self, gi, entries: list[dict], replicas: int, prefix: str, label: str,
                 poll: float, timeout: float, log=print):
        self.gi = gi
        self.entries = entries
        self.by_id = {e["id"]: e for e in entries}
        self.replicas = replicas
        self.prefix = prefix
        self.label = label
        self.poll = poll
        self.timeout = timeout
        self.log = log
        self.schemas = SchemaSource(gi=gi)
        self.tool_ids: dict[str, str] = {}
        self.src_histories: dict[str, str] = {}
        self.dataset_ids: dict[tuple[str, str], str] = {}
        self.collection_ids: dict[tuple[str, str], str] = {}
        # per replica state
        self.histories: dict[int, str] = {}
        self.state: dict[tuple[int, str], str] = {}        # pending/submitted/ok/failed/skipped
        self.jobs: dict[tuple[int, str], list[str]] = {}
        self.outputs: dict[tuple[int, str], dict] = {}     # name -> {"src", "id"}
        self.timeline: list[dict] = []
        self.errors: list[dict] = []

    # ---- resolution -------------------------------------------------------

    def resolve_tools(self):
        ids = self.schemas.all_ids()
        for e in self.entries:
            self.tool_ids[e["id"]] = resolve_tool_id(e["tool"], ids)
            self.log(f"  {e['id']:<28} -> {self.tool_ids[e['id']]}")

    def history_id(self, name: str) -> str:
        if name not in self.src_histories:
            matches = [h for h in self.gi.histories.get_histories(name=name) if not h.get("deleted")]
            if not matches:
                raise MatrixError(f"history {name!r} not found")
            self.src_histories[name] = matches[0]["id"]
        return self.src_histories[name]

    def lookup(self, history: str, ref: dict) -> dict:
        hid = self.history_id(history)
        if "dataset" in ref:
            key = (hid, ref["dataset"])
            if key not in self.dataset_ids:
                hits = [d for d in self.gi.histories.show_history(hid, contents=True, deleted=False,
                                                                     visible=True, types=["dataset"])
                        if d.get("name") == ref["dataset"] and d.get("state") == "ok"]
                if not hits:
                    raise MatrixError(f"dataset {ref['dataset']!r} not found (state ok) in history {history!r}")
                self.dataset_ids[key] = hits[-1]["id"]
            return {"src": "hda", "id": self.dataset_ids[key]}
        if "collection" in ref:
            key = (hid, ref["collection"])
            if key not in self.collection_ids:
                hits = [c for c in self.gi.histories.show_history(hid, contents=True, deleted=False,
                                                                     visible=True, types=["dataset_collection"])
                        if c.get("name") == ref["collection"]]
                if not hits:
                    raise MatrixError(f"collection {ref['collection']!r} not found in history {history!r}")
                self.collection_ids[key] = hits[-1]["id"]
            return {"src": "hdca", "id": self.collection_ids[key]}
        raise MatrixError(f"bad reference {ref!r}")

    def prefetch_inputs(self):
        for e in self.entries:
            for ref in iter_refs(e["inputs"]):
                if "dataset" in ref or "collection" in ref:
                    if "from" in ref:
                        continue
                    self.lookup(e["history"], ref)
        self.log(f"  resolved {len(self.dataset_ids)} datasets and {len(self.collection_ids)} collections")

    def materialize(self, replica: int, entry: dict, value):
        """Turn the matrix inputs tree into the flat dict bioblend's run_tool wants."""
        if isinstance(value, dict) and is_ref(value):
            if "batch" in value:
                inner = self.materialize(replica, entry, value["batch"])
                return {"batch": True, "values": [inner]}
            if "from" in value:
                outs = self.outputs.get((replica, value["from"])) or {}
                if "collection" in value:
                    want = value["collection"]
                    cands = {k: v for k, v in outs.items() if v["src"] == "hdca"}
                else:
                    want = value.get("output")
                    cands = {k: v for k, v in outs.items() if v["src"] == "hda"}
                if want is None:
                    if len(cands) != 1:
                        raise MatrixError(f"{entry['id']}: entry {value['from']!r} has outputs {sorted(outs)}; name one")
                    return next(iter(cands.values()))
                if want not in cands:
                    raise MatrixError(f"{entry['id']}: entry {value['from']!r} has no output {want!r}; has {sorted(outs)}")
                return cands[want]
            return self.lookup(entry["history"], value)
        if isinstance(value, list):
            return [self.materialize(replica, entry, v) for v in value]
        return value

    def flatten_inputs(self, tree: dict, replica: int, entry: dict) -> dict:
        flat = {}
        for key, value in tree.items():
            flat[key] = self.materialize(replica, entry, value)
        return flat

    # ---- scheduling -------------------------------------------------------

    def create_histories(self):
        for r in range(1, self.replicas + 1):
            name = f"{self.prefix}-r{r}" if self.replicas > 1 else self.prefix
            h = self.gi.histories.create_history(name=name)
            self.histories[r] = h["id"]
            for e in self.entries:
                self.state[(r, e["id"])] = "pending"
        self.log(f"  created {self.replicas} histor{'y' if self.replicas == 1 else 'ies'}")

    def ready(self, r: int, e: dict) -> bool:
        return self.state[(r, e["id"])] == "pending" and all(
            self.state[(r, d)] == "ok" for d in e["_deps"])

    def submit(self, r: int, e: dict):
        key = (r, e["id"])
        try:
            inputs = self.flatten_inputs(e["inputs"], r, e)
            result = self.gi.tools.run_tool(self.histories[r], self.tool_ids[e["id"]], inputs)
        except Exception as exc:  # noqa: BLE001 - record and continue the matrix
            self.state[key] = "failed"
            msg = str(exc)
            self.errors.append({"replica": r, "entry": e["id"], "tool": self.tool_ids[e["id"]],
                                "phase": "submit", "error": msg[:2000], "time": now_iso()})
            self.log(f"  r{r} {e['id']}: submit failed: {msg[:300]}")
            return
        jobs = [j["id"] for j in result.get("jobs", [])]
        outs: dict[str, dict] = {}
        for o in result.get("outputs", []):
            outs[o.get("output_name") or o["name"]] = {"src": "hda", "id": o["id"]}
        for c in result.get("output_collections", []) + result.get("implicit_collections", []):
            outs[c.get("output_name") or c["name"]] = {"src": "hdca", "id": c["id"]}
        self.jobs[key] = jobs
        self.outputs[key] = outs
        self.state[key] = "submitted" if jobs else "ok"
        self.timeline.append({"replica": r, "entry": e["id"], "tool": self.tool_ids[e["id"]],
                              "history_id": self.histories[r], "submitted": now_iso(), "jobs": jobs})

    def refresh(self) -> dict[str, str]:
        states: dict[str, str] = {}
        for hid in self.histories.values():
            for j in self.gi.jobs.get_jobs(history_id=hid):
                states[j["id"]] = j["state"]
        return states

    def run(self):
        start = time.time()
        self.log("Resolving tools")
        self.resolve_tools()
        self.log("Resolving inputs")
        self.prefetch_inputs()
        self.create_histories()
        self.log(f"Submitting {len(self.entries)} entries x {self.replicas} replicas")
        last_report = 0.0
        while True:
            # submit everything that is ready
            submitted = 0
            for r in range(1, self.replicas + 1):
                for e in self.entries:
                    if self.ready(r, e):
                        self.submit(r, e)
                        submitted += 1
            # settle states
            if any(s == "submitted" for s in self.state.values()):
                job_states = self.refresh()
                for key, jobs in self.jobs.items():
                    if self.state[key] != "submitted":
                        continue
                    st = [job_states.get(j, "unknown") for j in jobs]
                    if all(s == "ok" for s in st):
                        self.state[key] = "ok"
                    elif any(s in self.TERMINAL_BAD for s in st):
                        self.state[key] = "failed"
                        self.errors.append({"replica": key[0], "entry": key[1], "phase": "job",
                                            "states": st, "time": now_iso()})
            # skip dependents of failures
            for r in range(1, self.replicas + 1):
                for e in self.entries:
                    k = (r, e["id"])
                    if self.state[k] == "pending" and any(
                            self.state[(r, d)] in ("failed", "skipped") for d in e["_deps"]):
                        self.state[k] = "skipped"
            counts = defaultdict(int)
            for s in self.state.values():
                counts[s] += 1
            if time.time() - last_report > 60 or submitted:
                self.log(f"  [{int(time.time() - start):5d}s] " + " ".join(
                    f"{k}={counts[k]}" for k in ("pending", "submitted", "ok", "failed", "skipped")))
                last_report = time.time()
            if not counts["pending"] and not counts["submitted"]:
                break
            if time.time() - start > self.timeout:
                self.log("  timeout reached; leaving remaining jobs running")
                break
            time.sleep(self.poll)
        return time.time() - start

    # ---- metrics ----------------------------------------------------------

    def write_metrics(self, output_dir: str, matrix_files: list[str], elapsed: float):
        os.makedirs(output_dir, exist_ok=True)
        tool_of_job: dict[str, tuple[int, str]] = {}
        for (r, eid), jobs in self.jobs.items():
            for j in jobs:
                tool_of_job[j] = (r, eid)
        written = 0
        for r, hid in self.histories.items():
            for j in self.gi.jobs.get_jobs(history_id=hid):
                try:
                    job = self.gi.jobs.show_job(j["id"], full_details=True)
                    job["job_metrics"] = self.gi.jobs.get_metrics(j["id"])
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  could not fetch job {j['id']}: {exc}")
                    continue
                rep, eid = tool_of_job.get(j["id"], (r, None))
                entry = self.by_id.get(eid) if eid else None
                record = {
                    "run": self.label,
                    "cloud": "anvil",
                    "job_conf": None,
                    "workflow_id": f"tool-matrix:{eid}" if eid else "tool-matrix",
                    "history_id": hid,
                    "replica": rep,
                    "replicas": self.replicas,
                    "entry": eid,
                    "tool": entry["tool"] if entry else short_tool_name(job.get("tool_id", "")),
                    "inputs": entry["inputs"] if entry else None,
                    "metrics": job,
                }
                with open(os.path.join(output_dir, f"{j['id']}.json"), "w") as f:
                    json.dump(record, f, indent=2, default=str)
                written += 1
        summary = {
            "run": self.label,
            "replicas": self.replicas,
            "matrix_files": matrix_files,
            "entries": [{"id": e["id"], "tool": e["tool"], "tool_id": self.tool_ids.get(e["id"]),
                         "deps": e["_deps"]} for e in self.entries],
            "histories": {str(r): h for r, h in self.histories.items()},
            "states": {f"r{r}:{eid}": s for (r, eid), s in sorted(self.state.items())},
            "timeline": self.timeline,
            "errors": self.errors,
            "elapsed_seconds": round(elapsed, 1),
            "jobs_written": written,
        }
        with open(os.path.join(output_dir, "matrix-run.json"), "w") as f:
            json.dump(summary, f, indent=2, default=str)
        self.log(f"  wrote {written} job records to {output_dir}")
        return summary

    def purge_histories(self):
        """Purge the replica histories so a level's outputs do not stay on disk."""
        for r, hid in self.histories.items():
            try:
                self.gi.histories.delete_history(hid, purge=True)
            except Exception as exc:  # noqa: BLE001
                self.log(f"  could not purge history {hid}: {exc}")
        self.log(f"  purged {len(self.histories)} histor{'y' if len(self.histories) == 1 else 'ies'}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_validate(args) -> int:
    entries = load_matrix(args.matrix)
    source = SchemaSource(gi=connect(args) if not args.schema_dir else None, schema_dir=args.schema_dir)
    ids = source.all_ids()
    bad = 0
    resolved: dict[str, str] = {}
    for e in entries:
        try:
            resolved[e["id"]] = resolve_tool_id(e["tool"], ids)
        except MatrixError as exc:
            print(f"FAIL {e['id']}: {exc}")
            bad += 1
    for e in entries:
        tid = resolved.get(e["id"])
        if tid is None:
            continue
        problems = validate_entry(e, source.schema(tid), tid)
        problems += validate_refs(e, resolved, source)
        if problems:
            bad += 1
            print(f"FAIL {e['id']} ({tid})")
            for p in problems:
                print(f"     {p}")
        elif args.verbose:
            print(f"ok   {e['id']} ({tid})")
    print(f"{len(entries) - bad} of {len(entries)} entries valid")
    return 1 if bad else 0


def cmd_describe(args) -> int:
    source = SchemaSource(gi=connect(args) if not args.schema_dir else None, schema_dir=args.schema_dir)
    tid = resolve_tool_id(args.tool, source.all_ids())
    schema = source.schema(tid)
    print(f"{tid}")
    for key, alts in flatten_schema(schema.get("inputs") or []).items():
        for p in alts:
            extra = ""
            if p.get("type") == "select" and p.get("options") and not p.get("is_dynamic"):
                extra = " options=" + ",".join(str(o[1]) for o in p["options"][:8] if isinstance(o, (list, tuple)))
                if len(p["options"]) > 8:
                    extra += ",..."
            elif p.get("type") in ("data", "data_collection"):
                extra = f" formats={','.join(p.get('extensions') or [])}{' multiple' if p.get('multiple') else ''}"
            elif p.get("type") == "select" and p.get("is_dynamic"):
                extra = " (dynamic)"
            default = p.get("value")
            if default not in (None, "", [], {}):
                extra += f" default={default!r}"
            print(f"  {key:<55} {p.get('type')}{extra}")
    outs = schema.get("outputs") or []
    if outs:
        print("  outputs: " + ", ".join(f"{o['name']}({o.get('format')})" for o in outs))
    return 0


def cmd_plan(args) -> int:
    """Print the submission waves: which entries run together once their inputs exist."""
    entries = load_matrix(args.matrix)
    done: set[str] = set()
    wave = 0
    while len(done) < len(entries):
        ready = [e for e in entries if e["id"] not in done and all(d in done for d in e["_deps"])]
        wave += 1
        print(f"wave {wave}: {len(ready)} entries")
        for e in ready:
            deps = f"  <- {', '.join(e['_deps'])}" if e["_deps"] else ""
            print(f"  {e['id']:<22} {e['tool']}{deps}")
        done.update(e["id"] for e in ready)
    print(f"{len(entries)} entries, {len({e['tool'] for e in entries})} distinct tools, {wave} dependency waves")
    return 0


def cmd_run(args) -> int:
    entries = load_matrix(args.matrix)
    gi = connect(args)
    label = args.run_label or f"matrix-x{args.replicas}"
    prefix = args.history_prefix or f"{label}"
    runner = Runner(gi, entries, args.replicas, prefix, label, args.poll, args.timeout)
    elapsed = runner.run()
    summary = runner.write_metrics(args.output_dir, args.matrix, elapsed)
    if args.purge_histories:
        runner.purge_histories()
    states = defaultdict(int)
    for s in summary["states"].values():
        states[s] += 1
    print(f"done in {elapsed/60:.1f} min: " + " ".join(f"{k}={v}" for k, v in sorted(states.items())))
    return 0 if not states["failed"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--key")
    ap.add_argument("--profile", default="anvil", help="abm profile name (default anvil)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run")
    p.add_argument("matrix", nargs="+")
    p.add_argument("--replicas", type=int, default=1)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--history-prefix")
    p.add_argument("--run-label")
    p.add_argument("--poll", type=float, default=15.0)
    p.add_argument("--timeout", type=float, default=4 * 3600)
    p.add_argument("--purge-histories", action="store_true",
                   help="purge the replica histories after the metrics are written")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("plan")
    p.add_argument("matrix", nargs="+")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("validate")
    p.add_argument("matrix", nargs="+")
    p.add_argument("--schema-dir")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("describe")
    p.add_argument("tool")
    p.add_argument("--schema-dir")
    p.set_defaults(func=cmd_describe)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except MatrixError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
