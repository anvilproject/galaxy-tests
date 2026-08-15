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
or on the image for more details.

[![deployment history](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/deployments.svg)](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/deployments.html)

## Tool testing

After each successful deployment, automated tool tests run against the
instance using the AnVIL "cloud" tool set (loaded automatically via CVMFS at
startup). This is an end-to-end test of Galaxy's actual tool-execution path,
not just that it deploys.

**Latest run:** 300 tool tests run (6 error, 35 failure, 259 success).
[Full HTML report](https://htmlpreview.github.io/?https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/tool-tests/anvil-test-ci-260815-191104/results.html) &middot; [Tool list](https://github.com/anvilproject/galaxy-tests/blob/main/reports/anvil/tool-tests/anvil-test-ci-260815-191104/tools.yml)

### Longitudinal per-tool history

The [full per-tool test history](https://anvilproject.github.io/galaxy-tests/) shows pass/fail for each tool
test across the most recent runs, so a tool that starts failing (or a fix
that starts passing) is easy to spot over time.
