---
layout: default
title: Tool Test History
description: Longitudinal pass/fail history for the AnVIL "cloud" tool set, most recent 5 runs.
---

> **Preview page.** This is a design mockup, styled per the Galaxy Hub design
> guideline, with sample rows standing in for the real thing. The generator
> that populates this from actual `anvil-test.yaml` runs is later work (see
> `v2_plan.md`, milestone 4) - once a handful of real runs exist to chart.
> The [main README](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/README.md)
> links here once that's live.

Each row is one tool (id + version); each column is one of the 5 most recent
runs, newest first. Hover a cell for the run date and duration; click it to
open that run's full results.

<div class="gx-legend">
  <span class="gx-badge">🟩 pass</span>
  <span class="gx-badge">🟥 fail</span>
  <span class="gx-badge">— not run this cycle</span>
</div>

<div class="gx-table-wrap">

| Tool | Version | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 |
|---|---|:---:|:---:|:---:|:---:|:---:|
| bwa_mem2 | 2.2.1+galaxy0 | [🟩](# "Success, 41s") | [🟩](# "Success, 39s") | [🟩](# "Success, 44s") | [🟥](# "Failure: tool_stderr truncated - out of memory") | [🟩](# "Success, 42s") |
| deseq2 | 2.11.40.8+galaxy0 | [🟩](# "Success, 18s") | [🟩](# "Success, 17s") | [🟩](# "Success, 19s") | [🟩](# "Success, 18s") | [🟩](# "Success, 20s") |
| fastqc | 0.74+galaxy1 | [🟩](# "Success, 9s") | [🟩](# "Success, 9s") | [🟩](# "Success, 8s") | [🟩](# "Success, 9s") | [🟩](# "Success, 9s") |
| hisat2 | 2.2.1+galaxy1 | [🟥](# "Failure: exit code 1") | [🟥](# "Failure: exit code 1") | [🟩](# "Success, 112s") | [🟩](# "Success, 108s") | [🟩](# "Success, 115s") |
| salmon | 1.10.1+galaxy1 | [🟩](# "Success, 33s") | [🟩](# "Success, 31s") | [🟩](# "Success, 34s") | [🟩](# "Success, 33s") | — |
| unicycler | 0.5.0+galaxy2 | [🟩](# "Success, 612s") | [🟩](# "Success, 598s") | [🟩](# "Success, 604s") | [🟩](# "Success, 620s") | [🟩](# "Success, 611s") |

</div>
