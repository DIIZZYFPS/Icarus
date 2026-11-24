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


class DesktopPromptTests(unittest.TestCase):
    def setUp(self):
        from backend.agent.desktop_tools import desktop_prompt
        self.full = build_system_prompt(soul_content="Sharp colleague.")
        self.base = build_system_prompt(soul_content="Sharp colleague.", desktop=True)
        self.spoken = self.base + desktop_prompt()

    def test_spoken_turns_drop_the_text_channel_material(self):
        for text_only in ("MarkdownV2", "Telegram", "Max message length", "Code blocks when sharing code"):
            self.assertNotIn(text_only, self.base)
        self.assertLess(len(self.spoken), len(self.full) * .6)

    def test_personality_and_safety_rules_survive(self):
        self.assertIn("Sharp colleague.", self.base)
        self.assertIn("## End Soul Charter", self.base)
        self.assertIn("never instructions", self.base)           # web content is data
        self.assertIn("[GLOBAL]", self.base)                     # memory privacy
        self.assertIn("never write to main", self.base)
        self.assertIn("only when he explicitly asks", self.base)  # upgrades, stop_subagent

    def test_voice_block_comes_last_and_covers_speech_and_swearing(self):
        self.assertGreater(self.spoken.index("How you sound"), self.spoken.index("## End Soul Charter"))
        self.assertIn("Swearing is fine", self.spoken)
        self.assertIn("never censor it", self.spoken)
        self.assertIn("no symbols", self.spoken)

    def test_read_only_wins_over_desktop(self):
        prompt = build_system_prompt(read_only=True, desktop=True, soul_content="x")
        self.assertIn("## Access Level: Read-Only", prompt)
        self.assertNotIn("talking with him out loud", prompt)


if __name__ == "__main__":
    unittest.main()
