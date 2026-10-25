"""Thought-bubble bookkeeping in the renderer (drawing itself is checked by eye with --demo)."""

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AVATAR = os.path.join(HERE, "..", "renderer", "avatar.py")


def load_avatar():
    try:
        import gi  # noqa: F401  (the renderer needs GTK; skip where it isn't installed)
        spec = importlib.util.spec_from_file_location("avatar_under_test", AVATAR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


av = load_avatar()


@unittest.skipUnless(av is not None, "renderer needs GTK/PyGObject")
class BubbleTests(unittest.TestCase):
    def setUp(self):
        self.a = av.Avatar()

    def test_one_bubble_per_source_with_a_count(self):
        self.a.update_bubbles([{"source": "discord"}, {"source": "discord"}, {"source": "email"}, {"source": "weather"}], 10)
        self.assertEqual(sorted(self.a.bubbles), ["discord", "email"])
        self.assertEqual(self.a.bubbles["discord"]["count"], 2)
        self.assertNotEqual(self.a.bubbles["discord"]["slot"], self.a.bubbles["email"]["slot"])

    def test_ending_records_success_or_failure(self):
        self.a.update_bubbles([{"source": "discord"}, {"source": "subagent"}], 10)
        self.a.update_bubbles([{"source": "discord", "done": True, "ok": True},
                               {"source": "subagent", "done": True, "ok": False}], 12)
        self.assertEqual((self.a.bubbles["discord"]["end"], self.a.bubbles["discord"]["ok"]), (12, True))
        self.assertFalse(self.a.bubbles["subagent"]["ok"])

    def test_a_vanished_job_counts_as_ended(self):
        self.a.update_bubbles([{"source": "email"}], 10)
        self.a.update_bubbles([], 11)
        self.assertEqual(self.a.bubbles["email"]["end"], 11)

    def test_restarting_while_popping_makes_a_fresh_bubble_in_the_same_slot(self):
        self.a.update_bubbles([{"source": "email"}], 10)
        slot = self.a.bubbles["email"]["slot"]
        self.a.update_bubbles([], 12)
        self.a.update_bubbles([{"source": "email"}], 12.1)
        self.assertEqual((self.a.bubbles["email"]["end"], self.a.bubbles["email"]["seen"]), (None, 12.1))
        self.assertEqual(self.a.bubbles["email"]["slot"], slot)

    def test_quick_jobs_never_show(self):
        import cairo
        r = av.Renderer(self.a)
        self.a.update_bubbles([{"source": "discord"}], 10)
        self.a.update_bubbles([], 10.5)                     # done before BUBBLE_DELAY
        r.draw_bubbles(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, av.SW, av.SH)), 10.6)
        self.assertEqual(self.a.bubbles, {})


if __name__ == "__main__":
    unittest.main()
