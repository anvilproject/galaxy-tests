# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is not an application — it is a GitHub Actions test harness (plus the
data it produces) for the [AnVIL project](https://anvilproject.org/). It
periodically deploys Galaxy on a real GKE Kubernetes cluster via
[GalaxyKubeMan](https://github.com/galaxyproject/galaxykubeman-helm) and runs
the AnVIL default tool set against it, to catch deployment or tool-execution
regressions. There is no application code to build, lint, or unit test; the
"tests" are entire GitHub Actions workflows that provision cloud
infrastructure.

Because runs require a live GCP project (workload identity federation,
service account, GKE quota, secrets), workflow changes cannot be exercised
locally. The only ways to validate a change are (1) reading the YAML/Python
logic carefully, and (2) triggering a `workflow_dispatch` run on GitHub
Actions and inspecting the resulting cluster/job logs.

## Repository layout

- `.github/workflows/` — the GitHub Actions workflows. Two pipelines exist in
  parallel, each mirroring the other except for the target environment:
  - `new-edgetest.yaml` / `new-productiontest.yaml` — the current,
    actively-developed workflows (edge and production respectively). Both
    have their `schedule:` cron triggers commented out while under
    development, so they currently only run via `workflow_dispatch`.
  - `edgetest.yaml` / `tool-tests.yaml` — the older workflows they are
    replacing. `tool-tests.yaml`'s cron is disabled with a note that the file
    should eventually be deleted once the `new-*` workflows are trusted.
  - `.github/disabled/` — fully retired workflow variants kept only for
    reference; not executed by GitHub Actions.
- `.github/scripts/` — Python/bash helpers invoked as workflow steps (see
  Pipeline stages below for how they fit together).
- `.github/templates/` — Jinja2 templates for the generated README/HTML
  reports and Helm `values.yaml` files used to configure the GalaxyKubeMan /
  `galaxy-deps` chart installs.
- `production/anvil/tools.yaml` — the master list of AnVIL default tools
  (name, owner, revisions, tool shed, panel section). This is the source of
  truth that test chunking and tool installation both derive from.
- `production/anvil/cloud/*.yml` + matching `*.yml.lock` — per-panel-section
  installable tool subsets, generated from `tools.yaml` by
  `divide_sections.py` (`.yml.lock` keeps pinned revisions; `.yml` is the
  unpinned/no-tool_shed_url variant).
- `reports/anvil-edge/` and `reports/anvil-production/` — generated output,
  committed back to `main` by the workflows themselves (see below). Each
  contains `README.md`, `deployments.{json,html,svg}` (GKM install-time
  history), and `tool-tests/<run-prefix>/` subdirectories with per-run
  `tools.yml`, `results.{json,html,xunit}`, `chunk.json`, job/error/paused
  logs, etc. Treat these as CI-owned data, not something to hand-edit.
- `.abm/profile.yml` — a local/dummy [ABM](https://github.com/galaxyproject/gxabm)
  (`gxabm`) config; CI copies `.abm/` to `~/.abm` and overwrites the URL/key
  with the freshly deployed Galaxy's own values via `abm config`.

## Pipeline stages (per workflow run)

Each `new-edgetest.yaml` / `new-productiontest.yaml` run is three dependent
jobs:

1. **`deploygke`** — creates a single-node GKE cluster, named with a
   timestamp prefix (`edge-YY-MM-DD-HH-MM` / `prod-...`) that later steps and
   the `cleanup` job key off of.
2. **`testgalaxy1`** — the bulk of the work:
   - provisions two GCE disks (Postgres, NFS), installs `kubectl`/`helm`,
     deploys Galaxy dependencies then Galaxy itself via Helm using a rendered
     values file (`.github/templates/edge-values-template.yaml` for edge;
     inline `--set` flags for the older workflows).
   - records deployment success/duration via `report_deployment.sh`, which
     appends to `reports/<env>/deployments.json`, regenerates
     `deployments.svg` (matplotlib) and `deployments.html`
     (`report_deployment.py`), and **commits + pushes straight to `main`**.
   - configures the `abm` CLI against the new Galaxy instance (URL + freshly
     created API key).
   - selects a subset ("chunk") of `tools.yaml` to test this run via
     `get_chunk.py`: there are `NCHUNKS = 7 days * 2 runs/day = 14` chunks,
     normally selected by current weekday/AM-PM, or explicitly via the
     `chunk` `workflow_dispatch` input (valid range `[0:13]`).
   - runs `galaxy-tool-test` against the chunk via `run-galaxy-tests.sh`,
     collects job/error/paused summaries via `abm galaxy jobs ...`, builds
     the HTML/xunit report with `planemo test_reports`, regenerates
     `reports/<env>/README.md` via `update_readme.py` +
     `.github/templates/README.md.j2`, and again **commits + pushes to
     `main`** via `report_tests.sh`.
3. **`cleanup`** — runs with `if: always()`; deletes the GKE cluster and the
   two disks regardless of whether the test job succeeded.

Consequently, most commits on `main` (e.g. "Submitting anvil-production
report for deployment from ...", "Updating anvil-edge README for ...") are
CI-bot commits produced by this pipeline, not human work — this is expected
and by design, not repository noise.

## Working on this repo

- Prefer editing the `new-*` workflows over the deprecated `edgetest.yaml` /
  `tool-tests.yaml` unless specifically asked to touch the legacy path.
- `get_chunk.py` / `subset_tools.py` both implement chunk selection but are
  not identical — check which one a given workflow actually calls before
  assuming behavior.
- `reports/anvil-*/tools.yaml` (the chunking input) is meant to be a copy of
  `production/anvil/tools.yaml`, but the copy step is currently commented out
  in both `new-*` workflows — that file needs to exist/be kept in sync
  manually until that's re-enabled.
- `.github/scripts/analyze_firewall_rules.py`, `delete-stale-gke-rules.py`,
  and `cleanup-firewall-rules.sh` are standalone GCP housekeeping tools (see
  `.github/scripts/README-FIREWALL-CLEANUP.md`) for clearing stale/duplicate
  firewall rules left behind by repeated GKE cluster create/delete cycles;
  they are not part of the automated workflow runs.
