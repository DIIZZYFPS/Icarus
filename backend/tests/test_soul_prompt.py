import tempfile
import unittest
from pathlib import Path

from backend.agent.engine import build_system_prompt, load_soul


class SoulPromptTests(unittest.TestCase):
    def test_load_soul_reads_utf8_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "soul.md"
            path.write_text("## Core Principle\nSharp colleague.", encoding="utf-8")

            self.assertEqual(load_soul(path), "## Core Principle\nSharp colleague.")

    def test_missing_soul_is_non_fatal(self):
        self.assertEqual(load_soul(Path("/does/not/exist/soul.md")), "")

    def test_soul_is_injected_with_runtime_guardrails(self):
        prompt = build_system_prompt(
            soul_content="## Core Principle\nSharp colleague, not servant."
        )

        self.assertIn("## Soul — Operator-Approved Behavioral Charter", prompt)
        self.assertIn("Sharp colleague, not servant.", prompt)
        self.assertIn("Do not edit, replace, or weaken this document yourself.", prompt)
        self.assertIn("## End Soul Charter", prompt)

    def test_read_only_addendum_remains_after_soul(self):
        prompt = build_system_prompt(read_only=True, soul_content="Soul text")

        self.assertIn("## End Soul Charter", prompt)
        self.assertGreater(
            prompt.index("## Access Level: Read-Only"),
            prompt.index("## End Soul Charter"),
        )


if __name__ == "__main__":
    unittest.main()
