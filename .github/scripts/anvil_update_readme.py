"""Regenerate reports/anvil/README.md: deployment health plot + links to
the tool x run raster and the deploy-stage-timing chart (both served via
GitHub Pages).

Usage: anvil_update_readme.py <run_id> <run_html_url>
"""

import os
import sys

REPORT_DIR = "reports/anvil"
GITHUB_BASE = "https://github.com/anvilproject/galaxy-tests/blob/main"
RASTER_URL = "https://anvilproject.github.io/galaxy-tests/raster.html"
DEPLOY_STAGES_URL = "https://anvilproject.github.io/galaxy-tests/deploy-stages.html"


def main(run_id: str, run_html_url: str) -> None:
    tools_link = f"{GITHUB_BASE}/{REPORT_DIR}/tool-tests/{run_id}/tools.yml"

    content = f"""\
# Automated Tests for Galaxy on Kubernetes (AnVIL)

## Deployment testing

Weekly, Galaxy is deployed on a single GCE VM via
[galaxy-k8s-boot](https://github.com/galaxyproject/galaxy-k8s-boot), with tool
jobs executed on [GCP Batch](https://cloud.google.com/batch) (small jobs run on
the VM's own Kubernetes cluster instead - see
[job_conf.yml](https://github.com/galaxyproject/galaxy-k8s-boot/blob/dev/values/values.yml)).
The purpose of these tests is to provide reasonable confidence that Galaxy can
be deployed and is functional on AnVIL.

Below is a plot summarizing deploy successes and durations. [Click here]({GITHUB_BASE}/{REPORT_DIR}/deployments.html)
or on the image for more details. The [deploy-stage timing chart]({DEPLOY_STAGES_URL})
breaks each deployment down into the individual `ansible-pull` tasks that made
up its duration.

[![deployment history]({GITHUB_BASE}/{REPORT_DIR}/deployments.svg)]({GITHUB_BASE}/{REPORT_DIR}/deployments.html)

## Tool testing

After each successful deployment, automated tool tests run against the
instance using the AnVIL "cloud" tool set (loaded automatically via CVMFS at
startup). This is an end-to-end test of Galaxy's actual tool-execution path,
not just that it deploys. The [tool x run raster]({RASTER_URL}) shows
pass/fail/error for every tool across the run history, so a tool that starts
failing (or a fix that starts passing) is easy to spot over time - click a
cell for that tool's failing tests and tracebacks, the bar above a run column
for a run-wide breakdown, or a tool's name/recurrence bar for its history.
[Latest full HTML report]({run_html_url}) &middot; [latest tool list]({tools_link}).
"""
    with open(f"{REPORT_DIR}/README.md", "w") as f:
        f.write(content)
    print(f"Wrote {REPORT_DIR}/README.md")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
