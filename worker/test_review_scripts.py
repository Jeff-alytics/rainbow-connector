"""Guards for the two scripts that can write to production.

backfill-review-sunlight-v2.py and replay-review-sunlight-v2.py post through the
signed review-assessment callback when run with --apply, so they exercise the
same idempotency and matching path as the live lane. Their filenames contain
hyphens and they import boto3 at module scope, so they are loaded here by path
rather than imported; boto3 is pinned in the repository requirements.txt so this
runs in the same environment as the rest of the suite.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(stem: str):
    path = SCRIPTS / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewScriptSafetyTests(unittest.TestCase):
    def setUp(self):
        self.backfill = load("backfill-review-sunlight-v2")
        self.replay = load("replay-review-sunlight-v2")

    def test_default_preserves_the_natural_idempotency_key(self):
        # Re-running with no salt must be a no-op the callback counts as a
        # duplicate, rather than a second attachment.
        key = "a" * 64
        for module in (self.backfill, self.replay):
            self.assertEqual(module.salted_key(key, ""), key)

    def test_explicit_resalt_overrides_and_is_deterministic(self):
        key = "b" * 64
        for module in (self.backfill, self.replay):
            first = module.salted_key(key, "burned-key-recovery")
            self.assertNotEqual(first, key)
            self.assertEqual(first, module.salted_key(key, "burned-key-recovery"))
            # A different salt re-applies everything, which is why it must be named.
            self.assertNotEqual(first, module.salted_key(key, "burned-key-recovery-v2"))

    def test_a_full_page_is_refused_rather_than_silently_truncated(self):
        # loadGoEvents clamps at 1000 and the endpoint has no cursor, so a full
        # page means the window was cut short. Covering only the newest events
        # while reporting success is worse than refusing to run.
        for module in (self.backfill, self.replay):
            limit = module.EVENT_PAGE_LIMIT
            self.assertEqual(module.guard_not_truncated({"events": limit - 1}, "q")["events"], limit - 1)
            with self.assertRaises(SystemExit):
                module.guard_not_truncated({"events": limit}, "q")
            with self.assertRaises(SystemExit):
                module.guard_not_truncated({"events": limit + 5}, "q")

    def test_apply_and_resalt_are_opt_in_flags(self):
        # Neither writing nor overriding idempotency may be the default.
        for stem in ("backfill-review-sunlight-v2", "replay-review-sunlight-v2"):
            source = (SCRIPTS / f"{stem}.py").read_text(encoding="utf-8")
            self.assertIn('"--apply", action="store_true"', source)
            self.assertIn('"--resalt"', source)
            self.assertIn('default=""', source)


if __name__ == "__main__":
    unittest.main()
