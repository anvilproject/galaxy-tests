"""Regression checks for compact raster history retention."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


class RasterRetentionTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "raster", Path(__file__).with_name("anvil_generate_raster_data.py")
        )
        self.raster = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.raster)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, relative in {
            "TOOL_TESTS_DIR": "raw", "DOCS_TOOL_TESTS_DIR": "pages",
            "RASTER_DATA_DIR": "raster", "RUNS_DIR": "raster/runs",
            "MANIFEST_PATH": "raster/manifest.json", "MATRIX_PATH": "raster/matrix.json",
        }.items():
            setattr(self.raster, name, str(root / relative))
        self.run_id = "anvil-test-ci-261015-113110"
        self.raw = root / "raw" / self.run_id / "results.json"
        self.raw.parent.mkdir(parents=True)
        self.write_raw("success")

    def write_raw(self, status):
        self.raw.write_text(json.dumps({"tests": [{"data": {
            "tool_id": "example", "tool_version": "1", "test_index": 0,
            "status": status, "job": {
                "create_time": "2026-10-15T11:00:00",
                "update_time": "2026-10-15T11:01:00",
            },
        }}]}))

    def generate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.raster.main()
        return tuple(json.loads(Path(path).read_text()) for path in (
            self.raster.MANIFEST_PATH, self.raster.MATRIX_PATH
        ))

    def test_summary_survives_without_raw_or_details(self):
        expected = self.generate()
        self.raw.unlink()
        detail = Path(self.raster.RUNS_DIR) / self.run_id / "detail.json"
        detail.unlink()
        self.assertEqual(self.generate(), expected)
        self.assertFalse(detail.exists())
        self.assertEqual(expected[0][0]["duration_seconds"], 60)

    def test_raw_report_refreshes_existing_summary(self):
        self.generate()
        self.write_raw("failure")
        manifest, matrix = self.generate()
        self.assertEqual(manifest[0]["tests_failed"], 1)
        self.assertEqual(matrix["cells"]["example"][self.run_id]["status"], "fail")
        self.assertEqual(matrix["cells"]["example"][self.run_id]["versions"], ["1"])

    def test_archived_details_bootstrap_missing_summaries(self):
        expected = self.generate()
        self.raw.unlink()
        Path(self.raster.MANIFEST_PATH).unlink()
        Path(self.raster.MATRIX_PATH).unlink()
        manifest, matrix = self.generate()
        self.assertEqual(matrix, expected[1])
        self.assertEqual(manifest[0]["tests_passed"], 1)
        self.assertIsNone(manifest[0]["duration_seconds"])


if __name__ == "__main__":
    unittest.main()
