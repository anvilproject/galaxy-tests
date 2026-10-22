"""Disabled scheduled pins must stay inactive through dispatch and maintenance."""

import contextlib
import importlib.util
import io
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScheduledToolCommentsTest(unittest.TestCase):
    def test_version_bump_preserves_disabled_pin_and_explanation(self):
        bump = load_script("anvil_bump_scheduled_tool_versions")
        with tempfile.TemporaryDirectory() as directory:
            pinned = Path(directory) / "pinned.txt"
            available = Path(directory) / "available.txt"
            disabled = "# BUSCO paused for cost\n  # tools/busco/1.0\n\n"
            pinned.write_text(disabled + "tools/active/1.0\n")
            available.write_text("tools/busco/2.0\ntools/active/2.0\n")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                bump.main(str(pinned), str(available), write=True)
            self.assertEqual(pinned.read_text(), disabled + "tools/active/2.0\n")
            self.assertEqual(bump.read_ids(str(pinned)), ["tools/active/2.0"])
            self.assertIn("pinned 1 | bumped 1", output.getvalue())

    def test_sort_keeps_disabled_block_and_sorts_only_active_pins(self):
        sort = load_script("anvil_sort_scheduled_tool_ids")
        with tempfile.TemporaryDirectory() as directory:
            pinned = Path(directory) / "pinned.txt"
            disabled = "# BUSCO paused for cost\n# tools/busco/1.0\n\n"
            fast = "toolshed/repos/owner/fast/fast/1.0"
            slow = "toolshed/repos/owner/slow/slow/1.0"
            pinned.write_text(f"{fast}\n" + disabled + f"{slow}\n")
            with (
                patch.object(sort, "SCHEDULED_LIST_PATH", str(pinned)),
                patch.object(sort, "load_tool_rules", return_value=({}, {})),
                patch.object(sort, "measured_durations", return_value={sort.unversioned(fast): 1, sort.unversioned(slow): 10}),
                patch("sys.argv", ["sort", "unused-tools.yml"]),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                sort.main()
            self.assertEqual(pinned.read_text(), f"{slow}\n" + disabled + f"{fast}\n")
            self.assertIn("Sorted 2 tool IDs", output.getvalue())

    def test_workflow_filters_comments_and_blanks_without_splitting_ids(self):
        workflow = (SCRIPTS.parent / "workflows/anvil-test.yaml").read_text()
        patterns = re.findall(r"awk '([^']+)' .+ > /tmp/sampled_tool_ids.txt", workflow)
        self.assertEqual(len(patterns), 2, "Both scheduled and manual lists must be filtered")
        with tempfile.TemporaryDirectory() as directory:
            pinned = Path(directory) / "pinned.txt"
            pinned.write_text("# paused\n  # tools/busco/1.0\n\n \t\ntools/EMBOSS: compseq14/1.0\ntools/active/1.0\n")
            for pattern in patterns:
                result = subprocess.run(["awk", pattern, str(pinned)], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, "tools/EMBOSS: compseq14/1.0\ntools/active/1.0\n")


if __name__ == "__main__":
    unittest.main()
