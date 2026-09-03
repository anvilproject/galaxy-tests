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
one-line summaries for trend context), so it has nothing to hallucinate
a tool name or count from.

The one deliberate exception: incidents that are both NEW this run and
substantial (shared across multiple tools, or a meaningful single-tool
hit - see is_substantial_new()) also carry a bounded raw sample of the
actual captured error text, plus first_seen_run (has this exact
signature ever been recorded before, in the dashboard's full history).
Only for those does the prompt invite the model to go beyond stating
what happened and hypothesize a plausible *mechanism* from the error
text itself (e.g. a connection timeout to a specific host implies a
network-level problem reaching that host) - never a specific named
cause like a commit/config change, since the model isn't given
anything about recent changes to reason from. Minor, persistent,
continuing, and intermittent incidents never get a raw sample and are
kept to the brief "stable" framing only - see SYSTEM_INSTRUCTION for
the exact boundary.

When there's at least one such substantial-new incident, facts also
carries a small "run_level_signals" block - currently just a grep
count of "QueuePool limit" occurrences in that run's own
galaxy-web.log(.gz) (reports/anvil/deployments/<run_id>/), i.e. direct
evidence of server-side DB/connection-pool overload, independent of
any one incident. This is the same technique used to manually diagnose
this exact failure mode earlier (see galaxy-k8s-boot PR #115) - handing
the model that count (instead of raw log text) lets it distinguish
"the server was overloaded" from "something else" for network-shaped
errors without it inventing the distinction from nothing. The prompt
is explicit that this is the *only* extra signal available, and that
the model must not manufacture confidence beyond what it actually
supports - if the evidence doesn't clearly favor one explanation, it's
expected to say so rather than pick one anyway.

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
import gzip
import json
import os
import sys
from datetime import datetime, timezone

import requests

ANALYSIS_PATH_DEFAULT = "docs/analysis-data/analysis.json"
OUT_PATH_DEFAULT = "docs/analysis-data/brief-summary.json"
DEPLOYMENTS_DIR = "reports/anvil/deployments"
MODEL_DEFAULT = "gemini-3.6-flash"

SHARED_CAP = 8
PERSISTENT_CAP = 8
NEW_CAP = 8
RECOVERED_SAMPLE_CAP = 5
HISTORY_DEFAULT = 4

# A raw error sample is only attached (and elaboration only invited) for
# incidents meeting this "substantial" bar - see is_substantial_new().
# Everything shared already clears SHARED_TOOL_THRESHOLD (2 tools) in
# anvil_generate_analysis_data.py, so any *new* shared incident already
# qualifies; single-tool new issues need to hit this test count (or touch
# more than one version) to count as more than a one-off blip.
SUBSTANTIAL_NEW_ISOLATED_MIN_TESTS = 3
RAW_SAMPLE_CHARS = 600

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

Write plain prose, no bullet lists, no markdown, no incident IDs, no
code. Cover, in this priority order, and skip anything not applicable:
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
Keep items 1-3 to 2-4 sentences total.

Some entries under shared_incidents or new_isolated_issues carry a
"sample_raw" field: the actual captured error text for one
representative test case, plus "first_seen_run" (the earliest run in
this dashboard's full history where this exact signature was ever
recorded - if it equals this run's own run_id, it has genuinely never
happened before). This is only ever attached to an incident that is
both brand new this run AND substantial (affects multiple tools, or a
meaningful number of tests/versions for one tool) - it is never
attached to a persistent, continuing, or intermittent incident, and
those must NOT get this treatment even if you can guess something
about them. For each incident that DOES carry a sample_raw, add one
sentence in your main paragraph hypothesizing the most likely
technical *mechanism* behind it, grounded specifically in that text -
e.g. a connection timeout to a named host/IP suggests a network-level
problem reaching that host rather than a bug in the tool itself; a
database/connection-pool error suggests the server was overloaded; a
missing-file error suggests a data-staging problem. Phrase it
explicitly as a plausible read, not a confirmed diagnosis ("likely a
transient network issue between the test runner and the server" rather
than asserting it as fact) - and never claim a specific named cause (a
particular commit, deploy, or configuration change) since you are not
given anything about recent changes to reason from.

Optional second paragraph - deeper dive: if facts includes a top-level
"run_level_signals" object, you may write one additional short
paragraph (2-3 sentences) after the main one, trying to narrow down
*which* of the mechanisms you raised above is more likely, using
ONLY that object's evidence plus first_seen_run - nothing else. Right
now run_level_signals has exactly one fact:
queuepool_timeout_count - a count of database-connection-pool-
exhaustion errors elsewhere in this same run's server log, independent
of any specific incident. Reason like this: a nonzero (especially
high) queuepool_timeout_count is direct evidence the server itself was
overloaded during this run, which favors a "server overloaded"
explanation for network/timeout-shaped incidents over a "problem
reaching one external host" explanation; a zero count is evidence
AGAINST server overload (the server was otherwise keeping up), which
favors an external/network-path explanation instead; a null count
means the log wasn't available to check and you have no basis to favor
either explanation. Only write this second paragraph when the evidence
actually points somewhat one way - if it's ambiguous or absent
(run_level_signals missing, or queuepool_timeout_count is null), skip
the second paragraph entirely rather than manufacturing a conclusion
you don't have grounds for. Never state the favored explanation as
certain - "the absence of connection-pool errors elsewhere in this
run makes a server-overload explanation unlikely, pointing instead
toward a network-level problem reaching that host" is the right
register, not "this was caused by a network outage."

Use ONLY the facts given in the JSON, including sample_raw,
first_seen_run, and run_level_signals when present. Never invent a
tool name or count that is not present in the input, and never invent
a specific named cause per the paragraphs above. If there were no
failures or errors this run, say so in one sentence and stop there.
"""


def short_tool_name(tool_id: str) -> str:
    return tool_id.rsplit("/", 1)[-1]


def is_substantial_new(i: dict) -> bool:
    """New this run, and either shared (already >=2 tools per
    SHARED_TOOL_THRESHOLD upstream) or a single-tool hit big enough to be
    more than a one-off blip - the bar for attaching a raw sample and
    inviting the model to elaborate at all."""
    if i.get("state") != "New":
        return False
    if i.get("shared"):
        return True
    return (i.get("tests_affected") or 0) >= SUBSTANTIAL_NEW_ISOLATED_MIN_TESTS or (
        i.get("versions_affected") or 0
    ) > 1


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
    # See is_substantial_new()'s docstring and SYSTEM_INSTRUCTION for why
    # this is gated this tightly - a minor/persistent/continuing incident
    # must never carry this, or the model has grounds to elaborate on it.
    if is_substantial_new(i):
        raw = (i.get("sample") or {}).get("raw") or ""
        if raw:
            trimmed["sample_raw"] = raw[:RAW_SAMPLE_CHARS]
        trimmed["first_seen_run"] = i.get("first_seen_run")
    return trimmed


def count_queuepool_timeouts(run_id: str) -> int | None:
    """Direct evidence of server-side DB/connection-pool overload for this
    run, independent of any one incident - the same signal used to
    manually diagnose this failure mode via galaxy-k8s-boot PR #115.
    Returns None (not 0) when the log isn't available, so the caller can
    tell "checked, found none" apart from "couldn't check"."""
    for name in ("galaxy-web.log.gz", "galaxy-web.log"):
        path = os.path.join(DEPLOYMENTS_DIR, run_id, name)
        if not os.path.isfile(path):
            continue
        opener = gzip.open if path.endswith(".gz") else open
        count = 0
        with opener(path, "rt", errors="replace") as f:
            for line in f:
                if "QueuePool limit" in line:
                    count += 1
        return count
    return None


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

    shared_facts = [trim_incident(i) for i in target.get("shared_incidents", [])[:SHARED_CAP]]
    new_isolated_facts = [trim_incident(i) for i in target.get("new_isolated_issues", [])[:NEW_CAP]]

    facts = {
        "run_id": target["run_id"],
        "timestamp": target.get("timestamp"),
        "tools_attempted": target.get("tools_attempted"),
        "counts": target.get("counts"),
        "existing_algorithmic_summary_sentence": target.get("summary_sentence"),
        "shared_incidents": shared_facts,
        "persistent_tool_issues": [
            trim_incident(i) for i in target.get("persistent_tool_issues", [])[:PERSISTENT_CAP]
        ],
        "new_isolated_issues": new_isolated_facts,
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

    # Only computed/attached when there's actually something to explain -
    # no point grepping a log for a run with no substantial-new incidents.
    has_substantial_new = any("sample_raw" in i for i in shared_facts + new_isolated_facts)
    if has_substantial_new:
        qp_count = count_queuepool_timeouts(target["run_id"])
        facts["run_level_signals"] = {
            "queuepool_timeout_count": qp_count,
            "queuepool_timeout_count_note": (
                "Count of 'QueuePool limit' DB-connection-pool-exhaustion errors in this "
                "run's own galaxy-web.log - direct evidence of server-side overload, "
                "independent of any specific incident above. null means the log wasn't "
                "available to check, NOT that it was checked and found clean."
            ),
        }

    return facts


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
