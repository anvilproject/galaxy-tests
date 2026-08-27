# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is not an application — it is a GitHub Actions test harness (plus the
data it produces) for the [AnVIL project](https://anvilproject.org/). It
periodically deploys Galaxy on a real GCE VM via
[galaxy-k8s-boot](https://github.com/galaxyproject/galaxy-k8s-boot)'s
`anvil` branch (a single-node RKE2 cluster, with GCP Batch as an additional
elastic job-runner backend — not GKE/GalaxyKubeMan) and runs the AnVIL
default tool set against it, to catch deployment or tool-execution
regressions. There is no application code to build, lint, or unit test; the
"test" is an entire GitHub Actions workflow that provisions cloud
infrastructure.

Because runs require a live GCP project (workload identity federation,
service account, GCE quota, secrets), workflow changes cannot be exercised
locally. The only ways to validate a change are (1) reading the YAML/Python
logic carefully, and (2) triggering a `workflow_dispatch` run on GitHub
Actions and inspecting the resulting cluster/job logs. The generated
dashboards (`docs/*.html`) are the exception — see "Working on this repo"
below for previewing those locally.

## Repository layout

- `.github/workflows/anvil-test.yaml` — the only active workflow. One job:
  launch a GCE VM → wait for Galaxy to respond → run the tool-test suite →
  generate reports → commit them to `main` → delete the VM. Triggers on
  `workflow_dispatch` and a daily 1am ET `schedule`.
- `.github/scheduled-tool-ids.txt` — the fixed 200 tool IDs the scheduled
  (cron) run tests every night, committed so day-over-day results stay
  comparable while confidence builds before widening coverage. Manual
  `workflow_dispatch` runs ignore this file — they use the
  `random-tool-count`/`test-page-size` inputs instead, or default to
  testing every tool.
- `.github/scripts/anvil_*.py` — the report-generation scripts
  `anvil-test.yaml` calls: `anvil_record_deployment.py` (deploy
  success/duration), `anvil_generate_deploy_stages.py` /
  `anvil_generate_galaxy_startup_stages.py` (per-task Ansible and Galaxy
  startup timing, feeding `docs/deploy-stages.html`),
  `anvil_generate_raster_data.py` (feeds `docs/raster.html`), and
  `anvil_update_readme.py`.
- `docs/raster.html` / `docs/deploy-stages.html` — the two GitHub Pages
  dashboards (Jekyll `layout: default`, served from `docs/`): a per-tool ×
  per-run heatmap, and Ansible/Galaxy-startup stage timing respectively.
  Both read pre-generated JSON from `docs/raster-data/` / `docs/deploy-data/`
  (also committed by the workflow) rather than computing anything
  server-side.
- `docs/tool-tests/<run-prefix>/results.html` — per-run HTML test reports,
  linked from the raster page's per-cell inspector.
- `reports/anvil/` — the CI-owned data the dashboards are generated from:
  `deployments.{json,html,svg}` (deploy-timing history),
  `deployments/<run-prefix>/` (raw ansible-pull/Galaxy logs per run), and
  `tool-tests/<run-prefix>/` (`results.{json,html,xunit}`). Committed
  straight to `main` by the workflow itself (see Pipeline stages below) —
  treat as CI-owned, not something to hand-edit.
- `.github/disabled/`, plus the `production/anvil/tools.yaml`-driven
  chunking scripts (`get_chunk.py`, `subset_tools.py`, `divide_sections.py`)
  and the `.abm/` (`gxabm`) config — all leftover from the old
  GKE/GalaxyKubeMan pipeline (`new-edgetest.yaml` / `new-productiontest.yaml`
  / `tool-tests.yaml`, deleted; that pipeline drove Galaxy via the `abm` CLI
  and chunked `production/anvil/tools.yaml` across 14 scheduled slots).
  None of this is referenced by `anvil-test.yaml` (which mints its own API
  key via `bioblend` and gets its tool list from Galaxy's own
  `tests_summary` endpoint or the pinned file above) - kept only as
  historical reference, not part of any current workflow.
- `.github/scripts/analyze_firewall_rules.py`, `delete-stale-gke-rules.py`,
  and `cleanup-firewall-rules.sh` — standalone GCP housekeeping tools (see
  `.github/scripts/README-FIREWALL-CLEANUP.md`) for clearing stale/duplicate
  firewall rules left behind by repeated cluster create/delete cycles; not
  part of the automated workflow run.

## Pipeline stages (per workflow run)

`anvil-test.yaml` is one job with a linear sequence of steps:

1. Launch a GCE VM via `galaxy-k8s-boot`'s `bin/launch_vm.sh` (`dev` branch,
   `mixins/testing.yml` + `mixins/ci-concurrency.yml`, machine type
   `t2d-standard-8`) — a single-node RKE2 cluster with Galaxy deployed via
   Helm, TPV routing small jobs to that node's own k8s runner and larger
   ones to GCP Batch.
2. Wait for cloud-init/ansible-pull and the Galaxy API to respond; record
   deploy success/duration and Ansible/Galaxy-startup timing, all writing
   into a separate `/tmp/main-clone` of `main` (not the branch this
   workflow was dispatched from) so the commit step below never drags
   unrelated branch history in.
3. Mint a real API key via `bioblend`, then run the tool-test suite - which
   of five selection modes applies is priority-ordered (see the comment
   above the "Run tool tests" step): a scheduled run always uses the fixed
   list in `.github/scheduled-tool-ids.txt`; manual dispatch defaults to
   that same pinned list too (`tool-list-file`, so an unmodified manual
   dispatch tracks the scheduled set on demand), or can instead be pointed
   at a different repo-relative tool-list file, request a random sample
   (`random-tool-count`), a deterministic page (`test-page-size`/
   `test-page-number`), or default to testing everything if `tool-list-file`
   is cleared and none of the others are set.
4. Build the HTML/xunit report (`planemo test_reports`), regenerate the
   raster data and README, and **commit + push straight to `main`** from
   the separate clone.
5. Upload `results/`/`logs/` as a workflow artifact, then delete the VM
   (`if: always()`, regardless of whether the test step succeeded).

Consequently, most commits on `main` (e.g. "Update anvil reports from run
...") are CI-bot commits produced by this pipeline, not human work — this
is expected and by design, not repository noise.

## Working on this repo

- Analysis output does not belong in the repo. Planning notes, hypotheses,
  debugging write-ups, investigation summaries and similar working documents
  go in a personal, locally gitignored directory - not the repo root or
  `docs/` - and are never committed. This checkout's `.gitignore` names
  `ea-no-commit/` as that directory, but that's just one contributor's
  convention, not a repo requirement: a fresh clone doesn't need a folder
  with that exact name, just its own gitignored equivalent (add an entry
  for it to `.gitignore`). Only code, config and the CI-generated data
  under `reports/`/`docs/` belong in version control. `batch-issue.md` in
  the root predates this rule and will be moved out with the next change
  that touches it.
- Every `${{ inputs.* }}` reference in `anvil-test.yaml` needs a
  `|| <default>` fallback (see the top of the "Run tool tests" step):
  `inputs` is only populated for `workflow_dispatch`, so on the `schedule`
  trigger it's empty, and an unguarded reference silently evaluates to
  empty/falsy rather than erroring.
- To widen the scheduled run's coverage, regenerate
  `.github/scheduled-tool-ids.txt` with more sampled tool IDs (same
  `tests_summary` + `shuf` approach the `random-tool-count` manual mode
  uses) and commit it — don't switch the schedule to `random-tool-count`
  mode itself, which would sample a *different* random set every night and
  defeat the point of building day-over-day confidence in a stable set.
  Exclude Data Manager tools (`data_manager` in the tool path) from
  whatever you sample from - they don't test cleanly via
  `galaxy-tool-test` (confirmed live: a generic error with no per-test
  detail, or no report at all, while the underlying job - a real, often
  multi-hour index/database build - keeps running with nothing left to
  poll it, orphaned until GCP Batch's own 24h `max_run_duration` kills
  it). `random-tool-count`'s own sampling already filters these out for
  the same reason.
- `.github/scheduled-tool-ids.txt` is sorted so GCP-Batch-routed tools
  come first, local/k8s-routed tools after (`anvil_sort_scheduled_tool_ids.py`,
  using a checkout of https://github.com/galaxyproject/tpv-shared-database
  as the resource-requirement source of truth) - GCP Batch jobs are
  individually much slower than local ones, so submitting them first lets
  their long runtimes overlap the whole run instead of trickling in over
  the first ~30 minutes (see docs/parallelism-data for the measured
  effect). The script takes that checkout's `tools.yml` path as an
  argument - it isn't a submodule or fixed relative path, so a fresh
  clone needs its own local checkout of tpv-shared-database somewhere
  (e.g. alongside a personal gitignored directory like `ea-no-commit/`
  above, or anywhere else convenient) and to pass its own path in. This
  ordering is a point-in-time classification, not live TPV
  evaluation, and goes stale as tool versions/resource requirements/the
  shared DB change - re-run the sort script periodically, and always
  after regenerating/widening the list per the point above.
- `docs/raster.html` and `docs/deploy-stages.html` have no build step -
  they're plain HTML/CSS/JS rendered as GitHub Pages Liquid templates. To
  preview locally without Jekyll installed: parse the YAML front matter,
  splice the content into `docs/_layouts/default.html` in place of
  `{{ content }}`, and serve with `python3 -m http.server` alongside a copy
  of `docs/raster-data/`/`docs/deploy-data/`.
