"""Check summary retention without calling the Gemini API."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
