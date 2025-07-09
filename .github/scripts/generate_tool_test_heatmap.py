import os
import glob
import json
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import argparse
import re

REPORTS_DIR = "../../reports/anvil-production/tool-tests" #TODO update to dynamic path
README_PATH = os.path.join(REPORTS_DIR, "..", "README.md")
DEFAULT_OUTPUT_FILE = os.path.join(REPORTS_DIR, "..", "tool_test_heatmap.svg")


def parse_results_json(json_path):
    with open(json_path) as f:
        data = json.load(f)

    rows = []
    for test in data.get("tests", []):
        tool_id = test.get("data", {}).get("tool_id")
        status = test.get("data", {}).get("status")
        if tool_id and status:
            rows.append({
                "Tool ID": tool_id,
                "Status": status
            })

    df = pd.DataFrame(rows)
    if df.empty:
        print(f"⚠️ No valid rows parsed from {json_path}")
    return df


def collect_all_results():
    pattern = os.path.join(REPORTS_DIR, "gxy-auto-20[0-9][0-9]-*/results.json")
    report_paths = sorted(glob.glob(pattern))

    if not report_paths:
        raise FileNotFoundError(f"No JSON reports found at {pattern}. Please check REPORTS_DIR.")

    all_results = []
    for path in report_paths[-10:]:  # last 10 reports
        report_id = os.path.basename(os.path.dirname(path))
        df = parse_results_json(path)
        df["Report"] = report_id
        all_results.append(df)

    return pd.concat(all_results, ignore_index=True)


def compute_pass_rate(df):
    df["Pass"] = df["Status"].apply(lambda x: 1 if x == "success" else 0)
    grouped = df.groupby(["Tool ID", "Report"]).agg(total_tests=("Status", "count"), passes=("Pass", "sum"))
    grouped["Pass Rate"] = grouped["passes"] / grouped["total_tests"]
    pivot = grouped.reset_index().pivot(index="Tool ID", columns="Report", values="Pass Rate")
    return pivot.fillna(0)


def generate_discrete_heatmap(data, output_file):
    def bucketize(x):
        if x == 1.0:
            return "Perfect"
        elif x >= 0.75:
            return "High"
        elif x >= 0.5:
            return "Medium"
        elif x > 0.0:
            return "Low"
        else:
            return "Fail"

    bucketed = data.applymap(bucketize)
    cmap = {"Fail": "#d73027", "Low": "#fc8d59", "Medium": "#fee08b", "High": "#d9ef8b", "Perfect": "#1a9850"}
    color_data = bucketed.replace(cmap)

    plt.figure(figsize=(12, max(4, 0.3 * len(data))))
    sns.heatmap(data.isnull(), cbar=False, cmap=["white"])
    for y, row in enumerate(color_data.index):
        for x, col in enumerate(color_data.columns):
            val = color_data.loc[row, col]
            plt.gca().add_patch(plt.Rectangle((x, y), 1, 1, color=val))

    plt.xticks(ticks=[i + 0.5 for i in range(len(color_data.columns))], labels=color_data.columns, rotation=90)
    plt.yticks(ticks=[i + 0.5 for i in range(len(color_data.index))], labels=color_data.index, rotation=0)
    plt.title("Tool Test Health Heatmap (Color-coded Pass Rates)")
    plt.tight_layout()
    plt.savefig(output_file)
    print(f"Heatmap saved to {output_file}")
    return os.path.basename(output_file)


def generate_markdown_table(data):
    def emoji(val):
        if val == 1.0:
            return ":green_circle:"
        elif val >= 0.75:
            return ":yellow_circle:"
        elif val >= 0.5:
            return ":orange_circle:"
        elif val > 0:
            return ":red_circle:"
        else:
            return ":black_circle:"

    md = ["### Tool Test Health Table (Markdown Version)", "", "| Tool ID | " + " | ".join(data.columns) + " |", "|---|" + "|".join(["---"] * len(data.columns)) + "|"]
    for idx, row in data.iterrows():
        values = [emoji(row[col]) for col in data.columns]
        md.append(f"| {idx} | " + " | ".join(values) + " |")
    return "\n".join(md)


def append_to_readme(content, is_markdown=True):
    with open(README_PATH, "r") as f:
        existing = f.read()

    if is_markdown:
        new_md = generate_markdown_table(content)
        updated = re.sub(r"### Tool Test Health Table \(Markdown Version\)[\s\S]*?(\n\Z|\Z)", new_md + "\n", existing, flags=re.MULTILINE)
    else:
        image_ref = f"\n\n![Tool Test Heatmap]({content})\n"
        updated = existing + image_ref

    with open(README_PATH, "w") as f:
        f.write(updated)
    print(f"README.md updated at {README_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["svg", "md"], default="svg", help="Output format: svg or md")
    args = parser.parse_args()

    df = collect_all_results()
    pivot = compute_pass_rate(df)

    if args.format == "svg":
        fname = generate_discrete_heatmap(pivot, DEFAULT_OUTPUT_FILE)
        append_to_readme(fname, is_markdown=False)
    else:
        append_to_readme(pivot, is_markdown=True)
