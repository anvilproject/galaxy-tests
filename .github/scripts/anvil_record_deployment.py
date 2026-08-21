"""Append a deploy result to reports/anvil/deployments.json and
regenerate deployments.svg/deployments.html from the history.

Usage: anvil_record_deployment.py <status: success|failure> <duration_seconds> <run_url>
"""

import json
import os
import sys
from datetime import datetime, timezone

import matplotlib.pyplot as plt
from jinja2 import Template

REPORT_DIR = "reports/anvil"


def main(status: str, duration_seconds: float, run_url: str) -> None:
    os.makedirs(REPORT_DIR, exist_ok=True)
    deployments_path = f"{REPORT_DIR}/deployments.json"
    try:
        with open(deployments_path) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"results": {"deployments": []}}

    deploys = data["results"]["deployments"]
    deploys.insert(
        0,
        {
            "status": status,
            "duration_seconds": duration_seconds,
            "date": datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S %Y"),
            "run_url": run_url,
        },
    )
    data["results"]["deployments"] = deploys
    with open(deployments_path, "w") as f:
        json.dump(data, f, indent=4)

    recent = deploys[:30]
    dates = [datetime.strptime(d["date"], "%a %b %d %H:%M:%S %Y") for d in recent]
    minutes = [d["duration_seconds"] / 60.0 for d in recent]

    plt.figure(figsize=(12, 8), dpi=80)
    ax = plt.gca()
    ax.plot(dates, minutes, linestyle="-", marker=".", color="#25537b")
    plt.title("Galaxy (anvil) deploy time")
    plt.xlabel("Date of run")
    plt.ylabel("Deploy time (minutes)")
    plt.gcf().autofmt_xdate()
    plt.savefig(f"{REPORT_DIR}/deployments.svg")

    with open(".github/templates/deployments.html.j2") as f:
        template = Template(f.read())
    rows = "".join(
        f"<tr><td>{d['date']}</td><td>{d['status']}</td>"
        f"<td>{round(d['duration_seconds'] / 60.0, 1)} min</td>"
        f"<td><a href=\"{d['run_url']}\">run</a></td></tr>"
        for d in recent
    )
    table = f"<thead><tr><th>Date</th><th>Status</th><th>Duration</th><th>Run</th></tr></thead><tbody>{rows}</tbody>"
    with open(f"{REPORT_DIR}/deployments.html", "w") as f:
        f.write(template.render(table=table))


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]), sys.argv[3])
