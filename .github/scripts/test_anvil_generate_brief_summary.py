"""Check summary retention and deployment version fact building without calling the Gemini API."""

import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "brief", Path(__file__).with_name("anvil_generate_brief_summary.py")
)
brief = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brief)


class SummaryRetentionTest(unittest.TestCase):
    def test_legacy_migration_and_per_run_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief-summary.json"
            old = {"run_id": "old", "model": "old-model", "summary": "Original text"}
            new = {"run_id": "new", "model": brief.MODEL_DEFAULT, "summary": "New text"}
            path.write_text(json.dumps(old))
            brief.save_summary(str(path), new)
            self.assertEqual(json.loads(path.read_text())["summaries"], {"old": old, "new": new})
            revised = dict(new, summary="Revised text")
            brief.save_summary(str(path), revised)
            self.assertEqual(json.loads(path.read_text())["summaries"], {"old": old, "new": revised})

    def test_first_summary_creates_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "brief-summary.json"
            summary = {"run_id": "first", "summary": "First text"}
            brief.save_summary(str(path), summary)
            self.assertEqual(json.loads(path.read_text()), {"summaries": {"first": summary}})


class DeploymentVersionFactsTest(unittest.TestCase):
    def test_extract_versions_and_detect_changes_in_build_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deployments_dir = tmp_path / "deployments"
            tool_tests_dir = tmp_path / "tool-tests"

            for run_id, gx_ver, k8s_ver, chart_ver in [
                ("run-1", "26.1", "v1.36.2+rke2r1", "(role default)"),
                ("run-2", "26.1", "v1.36.2+rke2r1", "(role default)"),
                ("run-3", "26.2", "v1.36.2+rke2r1", "6.5.0"),
            ]:
                d_dir = deployments_dir / run_id
                t_dir = tool_tests_dir / run_id
                d_dir.mkdir(parents=True)
                t_dir.mkdir(parents=True)
                (t_dir / "results.json").write_text(
                    json.dumps({"tests": [{"data": {"job": {"galaxy_version": gx_ver}}}]})
                )
                with gzip.open(d_dir / "ingress-nginx.log.gz", "wt") as f:
                    f.write(
                        f'I1016 11:30:12.016867 7 main.go:248] "Running in Kubernetes cluster" '
                        f'major="1" minor="36" git="{k8s_ver}" state="clean"\n'
                    )
                (d_dir / "ansible-output.log").write_text(
                    f'ok: [localhost] => {{\n    "msg": [\n        "Galaxy Chart Version: {chart_ver}"\n    ]\n}}\n'
                )

            analysis = {
                "runs": [
                    {"run_id": "run-1", "counts": {"new": 0, "continuing": 0, "recovered": 0}},
                    {"run_id": "run-2", "counts": {"new": 0, "continuing": 0, "recovered": 0}},
                    {"run_id": "run-3", "counts": {"new": 1, "continuing": 0, "recovered": 0}},
                ]
            }

            with (
                mock.patch.object(brief, "DEPLOYMENTS_DIR", str(deployments_dir)),
                mock.patch.object(brief, "TOOL_TESTS_DIR", str(tool_tests_dir)),
            ):
                facts_unchanged = brief.build_facts(analysis, "run-2", 4)
                self.assertEqual(
                    facts_unchanged["deployment_versions"],
                    {"galaxy_version": "26.1", "k8s_version": "v1.36.2+rke2r1"},
                )
                self.assertEqual(facts_unchanged["deployment_version_changes"], {})

                facts_changed = brief.build_facts(analysis, "run-3", 4)
                self.assertEqual(
                    facts_changed["deployment_versions"],
                    {
                        "galaxy_version": "26.2",
                        "k8s_version": "v1.36.2+rke2r1",
                        "galaxy_chart_version": "6.5.0",
                    },
                )
                self.assertEqual(
                    facts_changed["deployment_version_changes"],
                    {
                        "galaxy_version": {
                            "previous": "26.1",
                            "current": "26.2",
                            "previous_run_id": "run-2",
                        }
                    },
                )


if __name__ == "__main__":
    unittest.main()
