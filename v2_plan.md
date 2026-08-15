# galaxy-tests v2 plan

Rewrite this repo's CI around the new `galaxy-k8s-boot` deployment model
(single GCE VM + GCP Batch for job execution) instead of GKE + GalaxyKubeMan,
and replace the edge/production split with a `dev`/`anvil` split that mirrors
`galaxy-k8s-boot`'s branch names. Only `anvil` is being built now; `dev` is
future work. Tests run against the whole AnVIL "cloud" tool set at once,
weekly, instead of in 14 twice-daily chunks — though whether "all at once"
survives contact with the real scale of that tool set is an open question
this plan flags rather than assumes (§5/§7). The tool set itself is no
longer vendored in this repo; it's fetched from `usegalaxy-tools` at run
time, since this repo's job is partly to confirm that tool set actually
loads on startup, not just to duplicate tracking it (§1/§3). Reporting keeps
the two signals the old repo had (deployment health, tool health), adds a
toolset-load-health signal, and adds the longitudinal per-tool view
prototyped in PR #2 and PR #29.

## 1. What gets dropped

- `.github/workflows/edgetest.yaml`, `new-edgetest.yaml`,
  `new-productiontest.yaml`, `tool-tests.yaml`, and all of
  `.github/disabled/` — deleted, not archived-in-place. Git history already
  preserves them if we ever need to look back.
- `production/anvil` vs. edge/production as separate deployment targets — GKM
  doesn't exist in the new model, so there's no "edge GKM version" to track
  separately from "production GKM version." Edge/production becomes
  dev/anvil (branches of `galaxy-k8s-boot`), and only `anvil` is implemented
  now.
- The chunking system: `get_chunk.py`, `subset_tools.py`, `chunk.txt`,
  `chunk.json`, `chunks.json`, and the per-chunk rows in
  `README.md.j2`/`update_readme.py`. `galaxy-tool-test` run with no `-t` flag
  tests every tool installed on the target instance (`-t` defaults to
  `ALL_TOOLS` — confirmed in
  `galaxy/tool_util/verify/script.py:510`), so there's no need to hand-split
  `production/anvil/tools.yaml` into chunks any more.
- `create_api_key.py` — no longer needed. `galaxy-k8s-boot` bakes a fixed
  bootstrap admin API key (`galaxypassword`, user
  `default-user@galaxyproject.org`) into the deployed instance, the same way
  `test-galaxy-gce.yml` in that repo already consumes it.
- `.github/scripts/analyze_firewall_rules.py`, `delete-stale-gke-rules.py`,
  `cleanup-firewall-rules.sh` — these existed because repeated GKE
  cluster/LoadBalancer create+delete cycles leaked firewall rules. A single
  VM per run doesn't create the same class of rules; drop unless the new
  workflow demonstrates otherwise.
- `production/anvil/tools.yaml`, `production/anvil/cloud/*.yml(.lock)`, and
  `divide_sections.py` entirely. **Correction from an earlier draft of this
  plan:** no tool-install step is needed at all (see §2 step 7 below), and
  this vendored copy is stale — it was forked from
  `galaxyproject/usegalaxy-tools`' `cloud/` directory (same filenames,
  e.g. `assembly.yml`, `bed.yml`) but hasn't tracked it: today `usegalaxy-tools/cloud`
  has 52 section files / 718 tools / 1823 pinned revisions, vs. 40 files /
  211 tools / 211 revisions here, and the files that exist in both differ.
  `production/anvil/tools.yaml`'s last real update was in 2021. Fetch the
  expected tool list from `usegalaxy-tools` directly at run time instead of
  vendoring a copy (see §3).

## 2. New deployment model

Adapted from `galaxy-k8s-boot`'s own
`.github/workflows/test-galaxy-gce.yml`, which already deploys+smoke-tests
Galaxy on GCE from GitHub Actions and is a working template:

1. Auth to GCP via the same Workload Identity Federation pattern the current
   workflows already use (`galaxy-tests-repo-actions-sa@anvil-and-terra-development.iam.gserviceaccount.com`).
   Confirm this SA has `compute.instances.{create,delete,get}` and whatever
   `launch_vm.sh` needs (it currently assumes `gcloud` is already
   authenticated with sufficient scope) — this is a different permission
   surface than the GKE cluster create/delete the SA was scoped for before.
2. Generate an ephemeral SSH keypair (as `test-galaxy-gce.yml` does).
3. Check out `galaxyproject/galaxy-k8s-boot` (public repo, no extra token
   needed) — either as a second `actions/checkout` step with
   `repository: galaxyproject/galaxy-k8s-boot`, or by driving `launch_vm.sh`
   directly against the upstream repo via its own `--git-repo`/`--git-branch`
   flags (it defaults to `GIT_BRANCH=anvil`, `GIT_REPO=` the upstream repo
   already, so for the `anvil` pipeline most of these flags are just the
   script's defaults).
4. `bin/launch_vm.sh -k "<pubkey>" --ephemeral-only <instance-name>` —
   `--ephemeral-only` (no persistent disks) matches the old "spin up fresh,
   tear down after" model and is documented as the intended CI/testing mode.
   Defaults already resolve to branch `anvil`, `values/values.yml`, and the
   current `galaxy-k8s-boot-v2026-06-30` machine image, so the anvil-pipeline
   invocation needs almost no explicit flags.
5. Poll `cloud-init status --wait` over SSH, then `kubectl rollout status`
   on the `galaxy` namespace deployments, then poll `GET /api/version` —
   same three-stage readiness check `test-galaxy-gce.yml` uses. Budget for
   this realistically: the README's "~6 minutes" figure is for the
   pre-built-image happy path only; `test-galaxy-gce.yml`'s own timeouts
   (30 min cloud-init poll + 15 min rollout + 5 min API poll) imply real
   margins closer to 30–45 minutes. Use those wider budgets, not the
   README's number.
6. Record deploy success/duration (see §6) the same way
   `report_deployment.sh`/`report_deployment.py` do today, just pointed at
   the new `reports/anvil/` tree.
7. **No tool-install step needed.** Correction from an earlier draft: the
   AnVIL/"cloud" tool set is not installed live by this pipeline at all. The
   `galaxy-helm` chart's `setupJob.downloadToolConfs` (enabled by default,
   `galaxy-helm/galaxy/values.yaml:269-286`) downloads a pre-built
   `contents.tar.gz` (or `partial.tar.gz`/`startup.tar.gz`) from
   `storage.googleapis.com/cloud-cvmfs/*` onto a shared volume mounted at
   `/cvmfs/cloud.galaxyproject.org`, and `tool_config_file`
   (`galaxy-helm/galaxy/values.yaml:641`) includes that mount's
   `shed_tool_conf.xml` automatically. That archive is built by a separate
   pipeline entirely outside this repo (presumably `usegalaxy-tools`' own
   CI, per its `.ci/` directory) and is explicitly documented as including
   test data ("Meant to be enough to run automated tool-tests, fully
   mimicking CVMFS setup" — `values.yaml:283-286`). So by the time Galaxy's
   API is reachable, the full "cloud" tool set is already loaded — nothing
   for this workflow to install.
   - This reframes part of this repo's actual purpose per the user: it's
     not just "do these tools run correctly," it's "did the toolset that's
     supposed to auto-load on startup actually load." That's a distinct
     failure mode (missing tool) from a functional test failure (tool
     present, ran, wrong output), and `galaxy-tool-test`'s `ALL_TOOLS`
     default (§5) can only test what's present — it can't tell you a tool
     silently failed to load. Add an explicit "expected vs. loaded" check:
     diff the tool ids from `usegalaxy-tools/cloud/*.yml.lock` (§3) against
     whatever Galaxy reports as installed (e.g. `/api/tools?in_panel=false`),
     and surface any gap as its own report signal, not just a test failure.
8. Run the full test suite (§5).
9. Generate reports, commit to `main` (§6).
10. Delete the VM in a step gated `if: always()`, mirroring
    `test-galaxy-gce.yml`'s cleanup step. Keep this step minimal/fast since
    steps still get a short grace period after a `timeout-minutes`
    cancellation, not the full step body.

Because there's only one VM (no separate cluster-create job + disk-create
job + helm-install job like the GKE pipeline had), this collapses cleanly
into a single job with sequential steps, rather than the old
`deploygke`/`testgalaxy1`/`cleanup` three-job split. Keep the `if: always()`
cleanup as the last step of that one job.

## 3. Toolset source of truth: fetch from `usegalaxy-tools`, don't vendor

`galaxyproject/usegalaxy-tools` (public repo) is the actual source that
produces the CVMFS `cloud.galaxyproject.org` archives Galaxy downloads at
startup (§2 step 7) — its `cloud/*.yml` files are hand-curated tool lists,
`cloud/*.yml.lock` are the machine-generated pinned-revision versions
(same schema as `production/anvil/tools.yaml` used today: `name`, `owner`,
`revisions`, `tool_panel_section_label`, `tool_shed_url`).

Instead of vendoring another copy into `galaxy-tests` (which is exactly how
`production/anvil/` went stale — last touched 2021, now less than a third
the size of upstream), the new workflow should check out
`galaxyproject/usegalaxy-tools` (or sparse-checkout just `cloud/`) at run
time and read `cloud/*.yml.lock` fresh on every run. This is what feeds
both the "expected vs. loaded" check in §2 step 7 and the per-section
parallelization split in §5.

## 4. GCP prerequisites — checked directly against `anvil-and-terra-development`, all satisfied

Everything `galaxy-k8s-boot`'s README lists as one-time GCP Batch setup
already exists in this project (verified via `gcloud`, not assumed):

- `galaxy-batch-runner@anvil-and-terra-development.iam.gserviceaccount.com`
  exists with `roles/batch.jobsEditor`, `roles/batch.agentReporter`,
  `roles/compute.instanceAdmin.v1`, `roles/iam.serviceAccountUser` — matches
  the README's requirement. (There's also a `galaxy-batch-vm` SA with
  `batch.jobsEditor`+`compute.viewer`, and a separate `galaxy-gcp-batch` SA —
  three similarly-named batch-related SAs exist; worth a sanity check during
  implementation that the right one is what `values/values.yml` actually
  wires up, but none of them are missing.)
- Firewall rules `allow-nfs-for-batch` (tcp/udp 2049, 111) and
  `allow-rabbitmq-for-batch` (tcp 5672) both exist, scoped to the internal
  VPC range and `k8s`-tagged instances.
- The Batch API (`batch.googleapis.com`) is enabled on the project.
- The default machine image (`galaxy-k8s-boot-v2026-06-30`) exists and is
  `READY`.
- Compute quota in `us-east4` (the `anvil` branch's default zone's region)
  is 5000 CPUs with only 10 in use — no meaningful quota ceiling on running
  many GCP Batch jobs concurrently, which meaningfully de-risks the §5/§7
  parallelism question (the constraint, if any, is more likely Galaxy's own
  job-dispatch throughput than GCP capacity).
- This repo's existing WIF setup — pool/provider
  `projects/526897014808/locations/global/workloadIdentityPools/galaxy-tests-identity-pool/providers/gxy-tests-provider`
  and service account `galaxy-tests-repo-actions-sa` (hardcoded in the
  current workflows, not a repo secret) — already has `roles/compute.admin`
  and `roles/editor` on the project, which covers creating/deleting the GCE
  VM. No new GCP identity setup needed; the new workflow reuses this as-is.
- `GIT_TOKEN` (repo secret, already present) still covers committing
  generated reports back to `main`, same as today.

**The one thing that is not yet in place: GitHub Pages isn't enabled for
this repo** (`gh api repos/anvilproject/galaxy-tests/pages` → 404), which
the §6 heatmap plan depends on (`docs/index.md` served via Pages). This
needs a maintainer to turn it on (Settings → Pages → Deploy from a branch →
`main` / `docs`) — it's a repo-admin action, not something to script around.

## 5. Test execution: the real scale is much bigger than first assumed

The first draft of this plan estimated timing from `production/anvil/tools.yaml`
(211 tools/revisions). Per §1/§3, that list is stale; the actual "cloud"
tool set it was supposed to mirror is **718 tools / 1823 pinned revisions**
in `usegalaxy-tools/cloud/*.yml.lock` today — roughly **8.6x more revisions**
than this repo currently tests. This changes the timing math enough that it
needs to be called out on its own, not folded quietly into a bigger number.

- Drop the per-tool-entry loop in `run-galaxy-tests.sh` and `get_chunk.py`'s
  chunk selection. `galaxy-tool-test`'s `-t/--tool-id` defaults to
  `ALL_TOOLS` (confirmed in `galaxy/tool_util/verify/script.py:510`), so a
  single invocation with no `-t` filter and `--parallel-tests N` tests
  everything currently loaded on the instance — no need to enumerate tool
  IDs at all for running tests (only for the §2/§6 "expected vs. loaded"
  check, which does need the full list from `usegalaxy-tools`).
- **Revised timing estimate, using the real 1823-revision figure:**
  reconstructing the current (stale, 211-revision) weekly cycle from
  `reports/anvil-production/tool-tests/chunks.json`, 211 revisions produced
  935 individual test cases (≈4.4 test cases/revision) totaling ~29,763
  sequential seconds; per-test averages ranged from ~32s (this specific
  snapshot) to ~74s (full historical average) depending on the window used.
  Scaling the 4.4-tests/revision ratio to 1823 revisions gives a **rough
  estimate of ~8,000 test cases**, or **~72–170 hours run sequentially** —
  i.e., the real suite is plausibly **12–28x** the 6-hour GitHub-hosted job
  cap before any parallelism, not comfortably under it as the earlier draft
  assumed. The tests-per-revision ratio itself is only a rough extrapolation
  from a small, possibly unrepresentative sample and needs to be checked
  against the real `usegalaxy-tools` set directly.
- Getting this under 6 hours therefore needs real parallelism, not just a
  `--parallel-tests` bump matching the current `job_conf.yml` `workers: 4`:
  even at `--parallel-tests 32` the low-end estimate is still ~2.3 hours and
  the high-end is ~5.3 hours — workable but with little margin, and
  `workers: 4` in `galaxy-k8s-boot/values/values.yml`'s `gcp_batch` runner
  would need to be raised to match via a CI-specific values overlay (
  `launch_vm.sh -f values/values.yml -f <ci-overrides.yml>`, which it
  already supports — multiple `-f`/`--values` flags, later files win).
  GCP Batch dispatches each job to its own ephemeral VM rather than a
  fixed-size node pool, so raising `workers` doesn't hit the same ceiling
  GKE did, but it does run into **GCP Batch/Compute Engine quota** (concurrent
  VM/CPU quota in the project) and per-job VM provisioning overhead, which
  is unmeasured for this workload and must be validated (§7).
- **Parallelize across GitHub Actions jobs too, not just within one
  `galaxy-tool-test` process.** `usegalaxy-tools/cloud/` is already
  naturally partitioned into 52 section files. Deploy the VM once, then fan
  out a matrix of jobs that each run `galaxy-tool-test` against the same
  Galaxy URL/key, scoped to one or a few sections' tool IDs (built from that
  section's `.yml.lock`), each with its own `--parallel-tests`. A final job
  merges all the matrix outputs (`merge_reports.py` already does something
  like this) before generating reports and tearing down the VM. This adds
  real parallelism headroom beyond what one process/one job can drive, at
  the cost of needing an aggregation step that doesn't exist today.
- Set a job-level `timeout-minutes` comfortably under 360 on every matrix
  job so a hung tool test fails cleanly instead of being silently killed at
  GitHub's 360-minute ceiling (a hard cap on GitHub-hosted runners, not
  raisable via `timeout-minutes`, confirmed current as of 2026).
- Keep `--retries` conservative (the old data shows at least one historical
  test recorded ~20,295s / 5.6 hours, almost certainly a hang against
  `GALAXY_TEST_DEFAULT_WAIT`, not real compute time — a single stuck tool
  must not be allowed to eat the whole budget).

## 6. Reporting: keep both signals, add the longitudinal view

Two existing signals carry over, retargeted from `reports/anvil-{edge,production}/`
to `reports/anvil/`:

1. **Deployment health** — `deployments.json/.svg/.html`, generated by
   `report_deployment.py` exactly as today, just one data point per week
   instead of two per day.
2. **Tool health, latest run** — the existing `results.json/.html/.xunit`
   for the most recent run, generated by `planemo test_reports` as today.

New signal (per §2 step 7): **toolset load health** — did every tool
`usegalaxy-tools/cloud/*.yml.lock` says should be installed actually show up
on the deployed instance. Surface this distinctly from tool-test pass/fail
(e.g. a short "N/1823 expected tools missing" line, listing which), since a
missing tool never gets a test result at all and would otherwise silently
disappear from the heatmap below instead of showing up as a problem.

New: a **longitudinal per-tool table**, based on the prototype in PR #29
(`generate_tool_test_heatmap.py`, branch `hujambo-dunia/galaxy-tests@viz-enhance-2`):
rows = tool id + version, columns = one per historical run (newest→oldest),
cells = a 🟩/🟥 emoji linking to that run's `results.json`, with the pass/fail
detail (duration, truncated error) as the link's title/tooltip. Adopt this
shape but:

- **Drop** the AI-error-summary add-on from that PR
  (`chat_lightweight.py` + the OpenAI-key-shaped `FEATURE_FLAG_SHOW_ERROR_SUMMARY`
  path) — external paid API dependency and a checked-in empty-secret
  placeholder, out of scope here.
- **Cap history depth at the last ~5 runs, per the user — not PR #29's
  approach.** PR #29's generated `docs/index.md` was already 7,000+ lines
  from just a handful of runs at the old, much smaller scale, and at the
  real full-suite scale (~1823 revisions, §5) an uncapped table would be
  roughly 8x that. Regardless of scale, only show the 5 most recent runs as
  columns — that's the longitudinal window that matters, not "as much
  history as fits." Older runs stay in `reports/anvil/tool-tests/<run-id>/`
  and remain individually linkable; they just drop out of the table.
- **Host it outside the README.** A table that size doesn't belong inline
  in `reports/anvil/README.md`. Reuse PR #29's `docs/index.md` +
  `docs/_layouts/default.html` GitHub Pages pattern for the full heatmap,
  and have `reports/anvil/README.md` (and the root `README.md` symlink,
  which currently points at `reports/anvil-production/README.md`) show the
  deployment plot, the latest run's summary, and a single prominent link
  to the full heatmap page.

## 7. Open risks that must be validated before locking in the weekly cron

These are explicitly *not* assumptions to build on silently — each needs a
real run against the new stack before trusting the schedule:

1. **Whether "everything, weekly, one run" fits at all.** This is now the
   central open question, not a secondary tuning detail. §5's rough estimate
   puts the real suite (1823 revisions) at ~72–170 sequential hours; even
   with aggressive parallelism (matrix jobs x `--parallel-tests`, `workers`
   raised well past 4) the projected range only comes down to something like
   2–5 hours with little margin, and that projection itself rests on a
   tests-per-revision ratio extrapolated from a small, possibly
   unrepresentative sample. Get a real number before committing to "no
   chunking" as a permanent design, and be ready to revisit the "run
   everything every week" requirement itself (e.g. GitHub-hosted vs.
   self-hosted runners — self-hosted allows 5-day jobs, sidestepping the
   6-hour cap entirely if that's an acceptable operational tradeoff) if the
   validated numbers don't fit any reasonable parallelism level.
2. **Real deploy+ready timing** on this specific job_conf/GCP Batch
   configuration — validate against the wider (30–45 min) budget in §2,
   not the README's "~6 minutes."
3. **`--parallel-tests` / matrix scaling** against a `gcp_batch`-backed
   instance specifically. GCP-side quota is confirmed generous (§4: 5000
   CPUs, 10 in use in `us-east4`), so this risk is really about Galaxy's own
   job-dispatch throughput (handler thread count, DB contention, GCP Batch
   API submission/polling overhead per job) at high `workers`/
   `--parallel-tests` values, not about running into a GCP ceiling.
4. **Fallback if it doesn't fit in 6 hours even with matrix parallelism:**
   in rough order of preference — (a) raise `workers`/`--parallel-tests`
   further (GCP-side headroom is confirmed, per §4), (b) move to a
   self-hosted runner for this workflow only, (c) as a last resort, split
   the *weekly* run into a small number of large, still-parallel-internally
   batches on a rotating schedule (e.g. half the tool set each of two
   runs/week) — prefer this over reverting to the old 14-slot chunk
   granularity, since it still keeps full coverage on a short, predictable
   cycle rather than the day/time-slot-dependent chunk selection being
   removed in §5.

## 8. Repo layout after the rewrite

```
.github/workflows/anvil-test.yaml     # new; deploy job + test matrix + report job
.github/scripts/                      # trimmed per §1; heatmap generator +
                                       # matrix-result-merge + expected-vs-loaded
                                       # check added
.github/templates/                    # README.md.j2 simplified (no chunk rows);
                                       # docs/_layouts/default.html added
docs/index.md                         # generated longitudinal heatmap (GitHub Pages)
reports/anvil/                        # new; replaces anvil-edge + anvil-production
  README.md
  deployments.{json,html,svg}
  tool-tests/<run-id>/{results.json,results.html,results.xunit,...}
reports/anvil-edge/, reports/anvil-production/   # left in place as historical
                                       # archive; no longer written to
README.md -> reports/anvil/README.md  # symlink retargeted
```

Note `production/anvil/` is gone entirely per §1/§3 — the expected tool list
is fetched from `usegalaxy-tools/cloud/*.yml.lock` at run time instead of
being vendored in this repo.

## 9. Implementation milestones

1. Author `.github/workflows/anvil-test.yaml` per §2/§5, `workflow_dispatch`
   only at first (no cron yet), so it can be run and timed manually. Start
   with a single non-matrix job (no `-t` filter, one `galaxy-tool-test` call)
   purely to get a first real timing number, before building out matrix
   parallelization.
2. Run it end-to-end a few times against the `anvil` branch of
   `galaxy-k8s-boot`; capture real numbers for deploy time and full-suite
   runtime at increasing `--parallel-tests` values. Resolve the open risks
   in §7 with real data before deciding whether/how to matrix-parallelize
   across jobs.
3. If needed per §7, build the matrix split (deploy job → N test-shard jobs
   over `usegalaxy-tools/cloud` sections → merge job) and the "expected vs.
   loaded" check from §2 step 7/§6.
4. Build the report generation: retarget `report_deployment.py`, trim
   `update_readme.py`/`README.md.j2` (drop chunk rows), add the heatmap
   generator (trimmed PR #29 script, capped to 5 run-columns per §6) and
   `docs/` output. Needs a maintainer to enable GitHub Pages for this repo
   first (§4) — flag this as a blocker for this milestone specifically, not
   the workflow itself.
5. Retarget the root `README.md` symlink; leave `reports/anvil-edge` and
   `reports/anvil-production` as read-only history.
6. Delete the workflows/scripts/vendored tool lists listed in §1.
7. Once the manual runs are trusted, add the weekly `schedule:` cron to
   `anvil-test.yaml`.
8. (Future, not this pass) add a `dev-test.yaml` mirroring `anvil-test.yaml`
   against `galaxy-k8s-boot`'s `dev` branch.
