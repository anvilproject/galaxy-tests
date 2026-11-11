"""Exercise the cleanup workflow's destructive selection with a fake gcloud."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


class CleanupTest(unittest.TestCase):
    def run_cleanup(self, instances="", list_status=0, delete_status=0):
        workflow = yaml.safe_load((WORKFLOWS / "anvil-cleanup.yaml").read_text())
        script = workflow["jobs"]["cleanup"]["steps"][-1]["run"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "gcloud"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['CALLS'], 'a') as f:\n"
                "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1:4] == ['compute', 'instances', 'list']:\n"
                "    print(os.environ['INSTANCES'], end='')\n"
                "    sys.exit(int(os.environ['LIST_STATUS']))\n"
                "sys.exit(int(os.environ['DELETE_STATUS']))\n"
            )
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                       RUN_ID="43024965993", CALLS=str(root / "calls"),
                       INSTANCES=instances, LIST_STATUS=str(list_status),
                       DELETE_STATUS=str(delete_status))
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                    env=env, capture_output=True, text=True)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            self.assertIn("--filter=labels.managed-by=galaxy-tests-ci AND labels.github-run-id=43024965993", calls[0])
            self.assertIn("--zones=us-east4-c", calls[0])
            return result, calls

    def test_no_vm_is_success_without_delete(self):
        result, calls = self.run_cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)

    def test_deletes_only_returned_names_as_separate_arguments(self):
        result, calls = self.run_cleanup("ci-vm-one\nci-vm-two\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[1], ["compute", "instances", "delete",
                                   "ci-vm-one", "ci-vm-two",
                                   "--project=anvil-and-terra-development",
                                   "--zone=us-east4-c", "--quiet"])

    def test_list_failure_never_deletes_even_with_partial_output(self):
        result, calls = self.run_cleanup("ci-vm-one\n", list_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)

    def test_delete_failure_is_reported(self):
        result, _ = self.run_cleanup("ci-vm-one\n", delete_status=1)
        self.assertNotEqual(result.returncode, 0)

    def test_both_deployment_workflows_use_independent_cleanup(self):
        for name in ("anvil-test.yaml", "anvil-deploy-and-test.yaml"):
            with self.subTest(workflow=name):
                data = yaml.safe_load((WORKFLOWS / name).read_text())
                cleanup = data["jobs"]["cleanup-vm"]
                self.assertEqual(cleanup["needs"], "deploy-and-test")
                self.assertEqual(cleanup["if"], "${{ always() }}")
                self.assertEqual(cleanup["uses"], "./.github/workflows/anvil-cleanup.yaml")


if __name__ == "__main__":
    unittest.main()
