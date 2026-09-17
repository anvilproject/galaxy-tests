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

- `.github/workflows/anvil-test.yaml` — the main workflow. One job:
  launch a GCE VM → wait for Galaxy to respond → run the tool-test suite →
  generate reports → commit them to `main` → delete the VM. Triggers on
  `workflow_dispatch` and a daily 1am ET `schedule`.
- `.github/workflows/update-scheduled-tool-versions.yaml` — a small weekly
  job (Wednesday 18:00 UTC, plus `workflow_dispatch` with a `dry-run`
  input) that re-pins `scheduled-tool-ids.txt` to the newest tool versions
  the instance offers and commits straight to `main`. It touches version
  segments only; adding or dropping tools stays a hand-made decision. See
  "Keeping the pinned list current" below.
- `.github/scheduled-tool-ids.txt` — the fixed ~200 tool IDs the scheduled
  (cron) run tests every night, committed so day-over-day results stay
  comparable while confidence builds before widening coverage. Manual
  `workflow_dispatch` runs ignore this file — they use the
  `random-tool-count`/`test-page-size` inputs instead, or default to
  testing every tool. Which *tools* it names is hand-curated; which
  *versions* it names is maintained automatically by the weekly job above.
- `reports/anvil/testable-tool-ids.txt` — the deployed instance's own
  `tests_summary` keys, rewritten by each run. The bump job reads it, and
  its diff is the only record of what the CVMFS tool bundle gained or lost
  on a given night.
- `.github/excluded-tool-ids.txt` — tools categorically excluded from runs,
  with the reason for each. Consulted when sampling the random pool, and
  reported on (never enforced) by the pinned-list pre-flight. Entries are
  version-independent fixed substrings so they survive regeneration of
  `scheduled-tool-ids.txt`. The bar is "cannot pass here no matter what we
  fix, and that is a property of the tool" — a tool merely failing today does
  not belong there, because exclusion hides it. The file also records, in
  comments, tools deliberately *not* excluded and why, so the same arguments
  are not relitigated.

  **When regenerating `scheduled-tool-ids.txt`** (periodically, at minimum to
  pick up newer tool versions): filter the candidate pool through this file
  first, e.g.

      grep -vE '^\s*(#|$)' .github/excluded-tool-ids.txt > /tmp/ex.txt
      grep -v -F -f /tmp/ex.txt /tmp/all_tool_ids.txt > /tmp/pool.txt

  then sample from `/tmp/pool.txt`. Re-read the exclusion file at the same
  time — entries name what would let a tool come back, and some are waiting
  on upstream fixes. Run `anvil_sort_scheduled_tool_ids.py` afterwards.
- `.github/scripts/anvil_*.py` — the report-generation scripts
  `anvil-test.yaml` calls: `anvil_record_deployment.py` (deploy
  success/duration), `anvil_generate_deploy_stages.py` /
  `anvil_generate_galaxy_startup_stages.py` (per-task Ansible and Galaxy
  startup timing, feeding `docs/deploy-stages.html`),
  `anvil_generate_raster_data.py` (feeds `docs/raster.html`), and
  `anvil_update_readme.py`.
- `.github/scripts/anvil_vm_net_sampler.sh` — runs detached *on the VM*
  (uploaded and started over SSH, collected before teardown) recording the
  connection-admission counters the runner cannot see: `syn_recv`,
  `ListenOverflows`, `ListenDrops`, `SyncookiesSent`, per-listener accept
  queues, conntrack and NIC drops. These move only once a SYN has reached
  the VM, which is what separates "dropped on the way" from "dropped by
  the VM" (§A5 Theory 2). Its output lands in
  `reports/anvil/deployments/<run-prefix>/vm-net.log.gz`, alongside
  `ingress-nginx.log.gz` (the public `hostNetwork` listener the runner
  actually connects to) and the pre-existing `galaxy-nginx.log.gz` (the
  chart's *internal* proxy, which only ever sees already-admitted
  requests). The conntrack rows are raw hex, summed during analysis —
  Debian's awk is mawk, which cannot parse hex.
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

## Where the tool set comes from

Nothing in this repo decides which tools the instance has. The chain, with
its cadences, is:

1. **usegalaxy-tools** (`galaxyproject/usegalaxy-tools`), the `cloud/`
   toolset — `.yml` files list tools, `.yml.lock` files pin toolshed
   changeset revisions. `update-cloud-repo.yml` runs Sunday 08:00 UTC and
   opens a PR; **merging it is manual**, so a published tool update can sit
   unmerged for days.
2. **CVMFS** `cloud.galaxyproject.org`, built from that toolset.
3. **cvmfs-cloud-clone** (`anvilproject/cvmfs-cloud-clone`) — daily 00:00
   UTC, mounts CVMFS and rsyncs it into three tarballs in the public
   `gs://cloud-cvmfs` bucket.
4. **galaxy-helm** fetches those at startup (`galaxy/values.yaml`,
   `cvmfs.archives`): `startup.tar.gz` (configs + tool XML),
   `partial.tar.gz` (adds tool scripts), and `contents.tar.gz` — the "full"
   one, the only tier carrying each tool's **test data**, which is what
   makes tool-testing here possible at all. A tool whose test data is
   missing from that archive falls through to the run's `--test-data`
   fallback paths.

So a tool revision published today typically reaches a nightly run several
days later, and the deployed instance is the only honest answer to "what
version is installed". Consequences that are easy to get wrong:

- **Tool updates are additive.** `usegalaxy-tools/scripts/update_tool.py`
  appends the newest revision to `revisions:` and never removes an older
  one. Old tool versions therefore stay installed indefinitely — a pinned
  version does not stop resolving when a newer one ships.
- **A lock file's `revisions:` list is sorted as hex strings, not
  chronologically.** The last entry is *not* the newest revision. To order
  them, intersect the list with the toolshed's
  `get_ordered_installable_revisions` for that repo, which is chronological.
- **QIIME2 is mostly not named in the lock files.** `cloud/qiime2.yml.lock`
  pins the single `suite_qiime2_core` repo, which pulls the individual
  `qiime2__*` repos in as dependencies; only some are named directly, in
  the separately-named `cloud/qiime_2.yml.lock`. Resolving a `q2d2` tool's
  version from the lock files alone will therefore come up empty for ~13 of
  the pinned IDs.
- **The toolshed API rate-limits.** Modest concurrency (8 workers) draws
  HTTP 429 within a couple hundred requests; back off and retry, or go
  serial, if scripting a bulk audit.

## Keeping the pinned list current

Tool updates reach the instance continuously (usegalaxy-tools' `cloud`
toolset → CVMFS → cvmfs-cloud-clone's daily bundle) and are *additive* —
`usegalaxy-tools/scripts/update_tool.py` appends the newest toolshed
revision and never removes older ones. So a pinned version never stops
resolving; it just keeps testing an old version while the newly published
one, the only one that actually changed, goes unexercised. That drift is
silent: the run stays green.

`update-scheduled-tool-versions.yaml` closes it weekly, running
`anvil_bump_scheduled_tool_versions.py` against
`reports/anvil/testable-tool-ids.txt` and then re-running the routing sort
(a version bump can change a tool's resource requirements, and so which
runner TPV picks). Deliberate constraints:

- **The instance, not the toolshed, is the source of truth.** The toolshed
  leads the deployed bundle by the whole publish chain, so its newest
  version is routinely one the instance does not have yet.
- **Version segments only.** Tools are never added or dropped; a pin whose
  tool has vanished is reported, not deleted.
- **Undecidable cases are skipped, not guessed.** `+galaxyN` is a PEP 440
  *local* segment, compared as a string, so plain `packaging` — and
  Galaxy's own `galaxy.tool_util.version.parse_version`, which inherits
  this — sorts `+galaxy10` *before* `+galaxy3`. The script compares the
  base with PEP 440 and the suffix with a natural ordering, and skips any
  tool whose base is not PEP 440 parseable (e.g. mmseqs2's `17-b804f`)
  rather than falling back to a guess. The failure mode is a missed bump,
  never a downgraded pin.
- **A stale inventory aborts the job.** The inventory only refreshes on a
  successful run, so a week of failed deploys is exactly when bumping
  could pin versions the instance no longer has.

Widening coverage is still the manual job described above: regenerate
`scheduled-tool-ids.txt` with more sampled tool IDs, filtered through
`excluded-tool-ids.txt`, then re-run the sort script.


## Reading a run's results

`reports/anvil/tool-tests/<run-prefix>/results.json` has a `tests` list whose
entries are keyed `{unversioned_tool_id}/{version}-{test_index}` — so a
prefix match against a pinned ID needs the trailing `-<n>`, not a `/`. Each
entry's `data` carries `tool_id` (unversioned) and `tool_version`
separately; prefer those over parsing the key.

`anvil_generate_raster_data.py` keys heatmap rows by that **unversioned**
`tool_id`, tracking versions inside the cell (`versions_total`,
`versions_affected`). A version bump therefore continues an existing row
rather than starting a new one, so re-pinning does not cost dashboard
history.

Baseline worth knowing before chasing a gap: roughly 9–11 of the pinned IDs
produce no results on any given night, and *which* ones varies run to run.
That is timeouts and flakes, not missing tool versions — a genuinely absent
version shows up in the pinned-list pre-flight instead.

## Working with live VMs

Two kinds turn up, and they have opposite rules. Check which one you have
before touching anything.

**Harness-run VM — observe only.** The VM a scheduled or dispatched run
deployed, named `anvil-test-ci-<run-prefix>`. A run in progress is an
experiment with results we intend to keep, so do not deploy to it, restart
pods, change config, or delete anything. Reading is fine and is usually the
point: `kubectl logs`, `kubectl exec` for read-only commands, API queries,
`gcloud compute ssh` to read a file. Its kubeconfig is generally
`/Users/ea/projects/galaxy-tests/ea-no-commit/test-vm-kubeconfig.yml`. It is
deleted at the end of the run, so anything needed after that has to be
collected before teardown - that is what the "Save ... under reports/anvil"
steps are for.

**`ea-dev` VM — full control.** A standing development instance, deployed
from galaxy-k8s-boot by hand and not tied to any run. Deploy, patch,
restart, create and delete pods, tear it down and rebuild it. This is where
candidate fixes get tried before they go anywhere near the harness. Its
kubeconfig is generally
`/Users/ea/projects/galaxy-k8s-boot/ea-no-commit/kubeconfig-vm`. Its size
and machine type vary as needed - unlike the harness VM, which is fixed at
`t2d-standard-16` in `anvil-test.yaml` - so do not read anything from its
shape as applying to a harness run.

**When a kubeconfig is stale.** These files are copies, and a redeployed VM
invalidates them (`Unable to connect to the server: dial tcp ...`). Do not
abandon the task: fetch a fresh one over SSH, e.g.

    gcloud compute ssh <instance> --project=anvil-and-terra-development \
      --zone=us-east4-c --strict-host-key-checking=no \
      --command="sudo cat /home/debian/.kube/config" \
      | sed "s|127.0.0.1|<external-ip>|" > <path>

RKE2 writes `127.0.0.1:6443` into that file, so the server address has to be
rewritten to the VM's external IP (this is what the workflow's "Copy
kubeconfig from VM" step does). If SSH is not available either, ask rather
than giving up.

## Diagnosing a run

- **Read the step's log text, not its conclusion.** Most diagnostic steps
  carry `continue-on-error: true`, so a step that failed still reports
  `success` in the run summary and in `gh run view`. A sampler that never
  started looked green for a whole night this way. Grep the job log for what
  the step actually printed.
- **Job logs are not downloadable while a run is in progress** - the API
  returns `BlobNotFound`. Either wait for the run to finish, or go to the VM
  and look directly.
- **Four connection-path views, and each answers a different question.** The
  runner sockets say a handshake failed; the VM sampler
  (`anvil_vm_net_sampler.sh`) says whether the SYN arrived at all;
  `ingress-nginx` says whether the connection was admitted at the public
  listener; `galaxy-nginx` only ever sees requests that were already
  admitted, so it cannot show a refused connection. Any conclusion about
  where a connection died needs at least two of them.
- **Prefer an in-run measurement to a standalone one.** Probes against an
  idle dev VM established almost nothing about §A5 because they changed
  egress, VM, load and time all at once. The probe that settled it ran as a
  step inside a real run, against the same VM, in the same minutes.
- **Mutation-test a new assertion.** Break the thing it covers and confirm
  the test fails. Several tests here asserted on their own configuration and
  would have passed against a reverted change.

## Notes and evidence

`ea-no-commit/outstanding.md` is the status document: what is true now.
Keep it to the conclusion and what to do next. The evidence, the theories
that were tested and discarded, and per-run measurements belong under
`ea-no-commit/details/<topic>/`, with a pointer from the summary - see
`details/README.md` for the folder map.

When a section outgrows its conclusion, move the history out rather than
letting it accumulate: §A5 reached ~530 lines, half the document, most of
it superseded. Date any section that records a theory, and mark it when it
is refuted - an undated "what remains unmeasured" reads as current long
after it stops being true.
