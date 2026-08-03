import tempfile
import unittest
from pathlib import Path

from scripts.build_bow_evaluation_targets import write_frozen_payload


class FrozenBowTargetWriterTests(unittest.TestCase):
    def test_refuses_to_replace_frozen_output_without_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "targets.json"
            write_frozen_payload({"version": 1}, output)
            with self.assertRaises(FileExistsError):
                write_frozen_payload({"version": 2}, output)
            write_frozen_payload({"version": 2}, output, overwrite=True)
            self.assertIn('"version": 2', output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
