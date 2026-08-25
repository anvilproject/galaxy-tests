# GCP Batch jobs and end-of-job metadata

Status as of the 2026-08-25 nightly run (`anvil-test-ci-260825-064657`).
Written for whoever picks up the Batch runner work; the numbers all come
from the committed `reports/anvil/` artifacts for that run and the one
before it.

## The original defect

Galaxy's `GoogleCloudBatchJobRunner` built its Batch task command with
`include_metadata=False`, so the remote task ran the tool and nothing else.
It never ran the metadata command and never wrote the
`<jobdir>/metadata/metadata_results_*` files that the default (`directory`)
metadata strategy expects the handler to read at finish time.

Measured on the 2026-08-24 run:

| | metadata read failed | jobs | rate |
|---|---|---|---|
| `gcp_batch` | 329 | 349 | **94%** |
| `k8s` | 0 | 337 | 0% |

Three things ruled out a filesystem or configuration cause:

- the handler read and moved Batch job *outputs* from those same job
  directories 2,444 times, so the NFS mount worked;
- the built job command was byte-identical in shape for Batch and k8s, and
  neither contained a metadata step;
- compute resources and machine types matched across runs, so the mixin
  merge was not dropping TPV params.

Because dynamic output discovery rides in the same end-of-job phase, this
did not present as "metadata is slightly off". It surfaced as
`Expected 36 lines in the output found 0`, `expected to have 2 elements, but
it had 0`, and `Unable to synchronously open file (file signature not
found)` — i.e. as apparent tool failures.

## The fix that landed

galaxyproject/galaxy#23361 (`d8fb1bac15`, "Set metadata externally in GCP
Batch runner finish_job") adds to `gcp_batch.py`:

```python
def finish_job(self, job_state: AsynchronousJobState) -> None:
    self._handle_metadata_externally(job_state.job_wrapper, resolve_requirements=True)
    super().finish_job(job_state)
```

This is a faithful copy of `KubernetesJobRunner.finish_job`, which does the
same two calls in the same order, and both runners pass
`include_metadata=False`.

It reached the deployment via the rolling `galaxy-min:26.1-auto` tag and is
demonstrably active in the 0825 run: `executing external set_meta script` on
Batch threads went **0 → 345** (one per Batch job), and metadata-read
failures went **760 → 24**. The original defect is addressed.

## What got worse in the same run

| | 0824 | 0825 |
|---|---|---|
| Tests | 795 | 795 |
| Passing | 647 | **330** |
| Failing | 103 | **420** |
| Empty-output failures | 18 | **231** |
| Content-diff failures | 17 | 116 |
| Upstream dataset unusable | 1 | 34 |
| Batch jobs with usable metrics | 215 / 349 | **27 / 345** |
| `finished: None` log lines | 0 | **5,397** |

327 tests went pass → fail. The failures are Batch-routed, and the diffs
show the *expected* side intact with the *actual* side empty
(`@@ -1,18 +0,0 @@`).

Ruled out as causes:

- **Not the client-side `--test-data` fallback** added the same day. Zero of
  165 failing tests had an expected-output filename colliding with either
  fetched directory, and that lookup only runs after the server has already
  failed to serve a file.
- **Not the `job_id_prefix` change** (galaxy-k8s-boot#101). It broke the
  reporting classifiers, now fixed, but the Helm merge preserves
  `cores`/`mem`/`docker_enabled` and the run's machine-type distribution is
  unchanged.
- **Not disk, NFS or memory exhaustion.** No `ENOSPC`, stale handle, or
  OOM-kill evidence in either log.

## Hypothesis

**The k8s ordering is not safe for Batch, because Batch outputs cross an
NFS boundary written by a remote host.**

`finish_job` runs `_handle_metadata_externally` *before*
`super().finish_job()` collects outputs. For Kubernetes that is fine: the
pod writes to the same PVC the handler reads, synchronously. For Batch the
task runs on its own GCE VM writing over NFS through an internal
LoadBalancer, so at the moment Galaxy observes the task complete, the
handler's NFS client may not yet see the output — attribute and data
caching are not coherent across those two clients.

If so, `set_meta` runs against absent or zero-length files and records empty
metadata; `super().finish_job()` then moves the real file, but the dataset's
recorded state is already wrong. Same code, different filesystem timing.

Two observations fit this and are hard to explain otherwise: output moves
stayed flat (2,444 → 2,481) while the datasets became empty — so the files
land, and something recorded before the move is wrong; and job *metrics*
degraded in step with metadata (215/349 → 27/345), which is the other thing
collected in that phase.

This is unproven. It predicts that an "empty" dataset's file on disk has
real content, which is the decisive test and needs a live cluster.

## Suggested next steps

1. **Check one empty dataset on disk.** If the file has content, this is an
   ordering/visibility bug, not a data-loss bug, and the fix is small.
2. **Do not revert #23361.** It fixes a real defect; the regression is
   plausibly in *when* it runs, not *what* it does.
3. **Add a visibility barrier before `set_meta` in the Batch runner** — re-stat
   the expected outputs, or collect outputs first — rather than inheriting the
   k8s ordering unchanged.
4. **Capture `gcp_batch_job_request.json` / `gcp_batch_job_params.json`.** The
   runner already writes both into every job directory and neither is in the
   artifacts. This has now blocked two investigations.
5. **Record the image digest per run.** `26.1-auto` is a rolling tag, so every
   run's Galaxy build is currently unknown after the fact. This regression was
   only attributable to a Galaxy change because someone remembered the PR.
