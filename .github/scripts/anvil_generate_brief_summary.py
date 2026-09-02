"""Generate docs/analysis-data/brief-summary.json: a short, plain-English
paragraph summarizing one run's docs/analysis-data/analysis.json (the
"Results brief" page's already-deduplicated/classified incident data),
via the Gemini API - so a viewer of results-brief.html gets a digestible
verdict up front instead of having to read the incident cards themselves.

Deliberately does NOT have the model cluster/classify anything from raw
test data - anvil_generate_analysis_data.py's signature clustering,
categorization (incident_rules.yaml) and New/Continuing/Persistent/
Intermittent/Recovered state machine stay exactly as-is and remain the
source of truth. This script only prose-ifies that already-computed,
already-correct structured data - the model gets a small, trimmed JSON
"facts" object (this run's top incidents/counts plus a few prior runs'
one-line summaries for trend context), not tracebacks, so it has nothing
to hallucinate a root cause, tool name, or count from.

Local testing: this script behaves identically locally and in CI - it
only needs GEMINI_API_KEY and a checked-out docs/analysis-data/analysis.json
(already committed on main), so you can iterate on the prompt/model/output
format entirely offline:

    export GEMINI_API_KEY=...
    python3 .github/scripts/anvil_generate_brief_summary.py

This overwrites docs/analysis-data/brief-summary.json and also prints the
summary to stdout, so you don't even need to open results-brief.html to
see the effect of a prompt tweak - `git diff`/`git checkout --` the output
file to compare or discard a local run. Pass --run-id to target an older
run already present in analysis.json (useful for testing against a run
with a specific, interesting incident mix without waiting for a new one).

Usage: anvil_generate_brief_summary.py [--run-id ID] [--history N]
       [--model NAME] [--analysis-json PATH] [--out PATH]
Env: GEMINI_API_KEY (required), GEMINI_MODEL (optional, overrides --model's
     own default)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

ANALYSIS_PATH_DEFAULT = "docs/analysis-data/analysis.json"
OUT_PATH_DEFAULT = "docs/analysis-data/brief-summary.json"
MODEL_DEFAULT = "gemini-3.6-flash"

SHARED_CAP = 8
PERSISTENT_CAP = 8
NEW_CAP = 8
RECOVERED_SAMPLE_CAP = 5
HISTORY_DEFAULT = 4

SYSTEM_INSTRUCTION = """\
You write a short, plain-English status summary for a nightly software
test dashboard, for engineers who do not want to read a table of test
results themselves. The input is a compact JSON object describing one
test run's failure/error incidents - already deduplicated and classified
by deterministic code, not by you - plus the last few runs' one-line
summaries for trend context.

Incident categories, already assigned, use exactly this vocabulary:
- Infrastructure: the deployment/test environment itself (e.g. data
  unavailable on the server, a service outage).
- Galaxy/test harness: the CI tooling or Galaxy's own internal utility
  jobs, not the biological tool being tested.
- Tool/runtime: the tool under test's own code, dependencies, or runtime
  environment.
- Test expectation/data: the test's fixture or expected output is stale
  or wrong, not a real regression.

The page already shows this run's raw new/continuing/recovered counts
and total tools/tests affected right next to where your summary appears
- do NOT open with, or otherwise restate, those totals (e.g. do not
write things like "N new issues and M continuing issues across K
tools"). Start directly with the substance instead.

Write 2-4 sentences of plain prose. No bullet lists, no markdown, no
incident IDs, no code. Cover, in this priority order, and skip anything
not applicable:
1. Anything new or large-and-shared that a person should actually look
   at, named specifically enough that a reader knows what it is without
   opening the page. A specific number tied to that one incident (e.g.
   "228 test failures across 43 tools") is fine and useful here - it's
   the redundant top-line totals above that should be skipped, not all
   numbers everywhere.
2. Known persistent issues: say "stable, no action needed" if their
   streak/count are essentially unchanged from recent runs, or flag it
   if it's growing.
3. Recovered issues, briefly, only if that meaningfully changes the
   picture (e.g. a big persistent one just cleared).

Use ONLY the facts given in the JSON. Never invent a root cause, tool
name, or count that is not present in the input. If there were no
failures or errors this run, say so in one sentence and stop there.
"""


def short_tool_name(tool_id: str) -> str:
    return tool_id.rsplit("/", 1)[-1]


def trim_incident(i: dict) -> dict:
    trimmed = {
        "title": i.get("title"),
        "category": i.get("category"),
        "state": i.get("state"),
        "tools_affected": i.get("tools_affected"),
        "fraction_of_toolset": i.get("fraction_of_toolset"),
        "tests_affected": i.get("tests_affected"),
        "streak": i.get("streak"),
        "versions_affected": i.get("versions_affected"),
    }
    # Name the tool when there's exactly one - the common case for
    # tool-specific (non-shared) incidents, and worth the extra context.
    # Skipped for genuinely shared incidents (dozens of tools) where no
    # single name is representative and the title is what matters.
    tools_detail = i.get("tools_detail") or []
    if len(tools_detail) == 1:
        trimmed["tool"] = short_tool_name(tools_detail[0]["tool_id"])
    return trimmed


def build_facts(analysis: dict, run_id: str | None, history: int) -> dict:
    runs = analysis["runs"]
    by_id = {r["run_id"]: idx for idx, r in enumerate(runs)}
    if run_id is None:
        idx = len(runs) - 1
    elif run_id in by_id:
        idx = by_id[run_id]
    else:
        raise SystemExit(f"run_id {run_id!r} not found in analysis.json")

    target = runs[idx]
    prior = runs[max(0, idx - history) : idx]

    recovered = target.get("recovered", [])
    recovered_sample = [
        {"title": r.get("title"), "tool": short_tool_name(r["tool"]), "category": r.get("category")}
        for r in recovered[:RECOVERED_SAMPLE_CAP]
    ]

    return {
        "run_id": target["run_id"],
        "timestamp": target.get("timestamp"),
        "tools_attempted": target.get("tools_attempted"),
        "counts": target.get("counts"),
        "existing_algorithmic_summary_sentence": target.get("summary_sentence"),
        "shared_incidents": [trim_incident(i) for i in target.get("shared_incidents", [])[:SHARED_CAP]],
        "persistent_tool_issues": [
            trim_incident(i) for i in target.get("persistent_tool_issues", [])[:PERSISTENT_CAP]
        ],
        "new_isolated_issues": [trim_incident(i) for i in target.get("new_isolated_issues", [])[:NEW_CAP]],
        "recovered_count": len(recovered),
        "recovered_sample": recovered_sample,
        "recent_runs_for_trend": [
            {
                "run_id": r["run_id"],
                "timestamp": r.get("timestamp"),
                "summary_sentence": r.get("summary_sentence"),
                "counts": r.get("counts"),
            }
            for r in prior
        ],
    }


def call_gemini(facts: dict, model: str, api_key: str) -> str:
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        json={
            "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(facts, indent=2)}]}],
            "generationConfig": {"temperature": 0.2},
        },
        timeout=60,
    )
    if not resp.ok:
        raise SystemExit(f"Gemini API error {resp.status_code}: {resp.text[:2000]}")
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        raise SystemExit(f"Unexpected Gemini response shape: {json.dumps(data)[:2000]}") from e


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=None, help="Target run (default: latest in analysis.json)")
    parser.add_argument("--history", type=int, default=HISTORY_DEFAULT, help="Prior runs to include for trend context")
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", MODEL_DEFAULT))
    parser.add_argument("--analysis-json", default=ANALYSIS_PATH_DEFAULT)
    parser.add_argument("--out", default=OUT_PATH_DEFAULT)
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not set")

    with open(args.analysis_json) as f:
        analysis = json.load(f)

    facts = build_facts(analysis, args.run_id, args.history)
    summary = call_gemini(facts, args.model, api_key)

    print(summary)
    print(file=sys.stderr)
    print(f"({len(summary)} chars, model={args.model}, run_id={facts['run_id']})", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(
            {
                "run_id": facts["run_id"],
                "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "model": args.model,
                "summary": summary,
            },
            f,
            indent=2,
        )
    print(f"Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
