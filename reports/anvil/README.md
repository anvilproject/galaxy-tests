# Automated Tests for Galaxy on Kubernetes (AnVIL)

## Deployment testing

Weekly, Galaxy is deployed on a single GCE VM via
[galaxy-k8s-boot](https://github.com/galaxyproject/galaxy-k8s-boot), with tool
jobs executed on [GCP Batch](https://cloud.google.com/batch) (small jobs run on
the VM's own Kubernetes cluster instead - see
[job_conf.yml](https://github.com/galaxyproject/galaxy-k8s-boot/blob/dev/values/values.yml)).
The purpose of these tests is to provide reasonable confidence that Galaxy can
be deployed and is functional on AnVIL.

Below is a plot summarizing deploy successes and durations. [Click here](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/deployments.html)
or on the image for more details. The [deploy-stage timing chart](https://anvilproject.github.io/galaxy-tests/deploy-stages.html)
breaks each deployment down into the individual `ansible-pull` tasks that made
up its duration.

[![deployment history](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/deployments.svg)](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/deployments.html)

## Tool testing

After each successful deployment, automated tool tests run against the
instance using the AnVIL "cloud" tool set (loaded automatically via CVMFS at
startup). This is an end-to-end test of Galaxy's actual tool-execution path,
not just that it deploys. The [tool x run raster](https://anvilproject.github.io/galaxy-tests/raster.html) shows
pass/fail/error for every tool across the run history, so a tool that starts
failing (or a fix that starts passing) is easy to spot over time - click a
cell for that tool's failing tests and tracebacks, the bar above a run column
for a run-wide breakdown, or a tool's name/recurrence bar for its history.
[Latest full HTML report](https://anvilproject.github.io/galaxy-tests/tool-tests/anvil-test-ci-260902-111925/results.html) &middot; [latest tool list](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/tool-tests/anvil-test-ci-260902-111925/tools.yml).
