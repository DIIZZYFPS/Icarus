"""Claude Code tasks from the desktop, calendar reminders, and Google token-expiry alerts."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from backend.agent import calendar_watcher, claude_runner, desktop_notify, desktop_session, desktop_tools
from backend.agent import google_auth_health


class FakeRedis:
    def __init__(self, receivers=1):
        self.published, self.keys, self.receivers = [], {}, receivers

    async def publish(self, channel, payload):
        self.published.append((channel, json.loads(payload)))
        return self.receivers

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


class RunClaudeTaskToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.answer = {"ok": True, "result": "confirmed"}

        async def send(message):
            pass

        self.session = desktop_session.DesktopSession(send)

        async def fake_call(name, args, timeout=None):
            self.calls.append((name, args, timeout))
            return self.answer

        self.session.call = fake_call
        desktop_session.set_active(self.session)
        self.addCleanup(desktop_session.clear_active, self.session)
        self.redis = FakeRedis()
        for p in (
            mock.patch.dict(os.environ, {"DESKTOP_CLAUDE_PROJECTS": "icarus=~/Projects/Icarus,site=~/code/site"}),
            mock.patch("backend.database.redis_connection.get_redis_client", lambda: self.redis),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_ask_mode_dispatches_without_confirmation(self):
        result = await desktop_tools.run_claude_task("Icarus", "Why does test_pause_resume flake?", "ask")
        self.assertEqual(self.calls, [])
        channel, payload = self.redis.published[-1]
        self.assertEqual(channel, "icarus:councilor:requests")
        self.assertEqual((payload["type"], payload["project"], payload["mode"]), ("claude_code", "icarus", "ask"))
        self.assertIn(payload["task_id"], result)

    async def test_edit_mode_asks_first_and_respects_a_no(self):
        self.answer = {"ok": False, "result": "declined"}
        result = await desktop_tools.run_claude_task("icarus", "Fix the flaky test", "edit")
        self.assertEqual(self.calls[0][0], "confirm")
        self.assertIn("Edit icarus with Claude?", self.calls[0][1]["title"])
        self.assertIn("declined", result)
        self.assertEqual(self.redis.published, [])

    async def test_edit_mode_confirmed_names_the_branch(self):
        result = await desktop_tools.run_claude_task("icarus", "Fix the flaky test", "edit")
        payload = self.redis.published[-1][1]
        self.assertIn(f"claude/{payload['task_id']}", result)

    async def test_unknown_project_and_offline_councilor(self):
        self.assertIn("Unknown project", await desktop_tools.run_claude_task("photoshop", "x", "ask"))
        self.redis.receivers = 0
        self.assertIn("isn't running", await desktop_tools.run_claude_task("site", "explain the build", "ask"))

    def test_prompt_lists_projects_and_steers_coding_to_claude(self):
        with mock.patch.dict(os.environ, {"DESKTOP_CLAUDE_PROJECTS": "icarus=/x"}):
            prompt = desktop_tools.desktop_prompt()
        self.assertIn("run_claude_task, not escalate_to_councilor", prompt)
        self.assertIn("Projects: icarus.", prompt)


class ClaudeRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_use_labels(self):
        d = claude_runner.describe_tool_use
        self.assertEqual(d("Edit", {"file_path": "/a/b/engine.py"}), "Editing engine.py")
        self.assertEqual(d("Bash", {"command": "python -m unittest discover\nexit"}), "Running python -m unittest discover")
        self.assertEqual(d("Grep", {"pattern": "x"}), "Searching the code")

    def test_parse_projects(self):
        self.assertEqual(claude_runner.parse_projects("Icarus=/p/i, bad, site = /p/s"),
                         {"icarus": Path("/p/i"), "site": Path("/p/s")})

    async def _edit_run(self, d, *, script="echo \"print('fixed')\" > app.py", result_text="Fixed the greeting.",
                        before=None, env=None):
        """Init a repo at d/repo, run edit mode with a fake claude that runs `script` in the worktree.
        $REPO in the script is the main checkout (to simulate DIIZZY working during the run)."""
        repo, worktrees = Path(d, "repo"), Path(d, "wt")
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "app.py").write_text("print('hi')\n")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        if before:
            before(repo)
        fake = Path(d, "claude")
        fake.write_text(textwrap.dedent("""\
            #!/bin/sh
            REPO=%s
            echo '{"type":"system","subtype":"init"}'
            echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit","input":{"file_path":"app.py"}}]}}'
            %s
            echo '{"type":"result","subtype":"success","is_error":false,"result":%s,"num_turns":2,"total_cost_usd":0.01}'
            """) % (repo, script, json.dumps(result_text)))
        fake.chmod(0o755)
        self.progress = []

        async def on_progress(text):
            self.progress.append(text)

        with mock.patch.dict(os.environ, {"CLAUDE_BIN": str(fake), "CLAUDE_WORKTREE_ROOT": str(worktrees), **(env or {})}):
            result = await claude_runner.run_claude(task_id="sub-1-abc123", project_name="repo", project=repo,
                                                    task="Fix the greeting", mode="edit", on_progress=on_progress)
        return repo, result

    @staticmethod
    def _branches(repo):
        return subprocess.run(["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.split()

    @staticmethod
    def _log(repo, ref="HEAD"):
        return subprocess.run(["git", "-C", str(repo), "log", "--format=%s", ref], capture_output=True, text=True).stdout

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_edit_mode_applies_the_commit_to_the_checked_out_branch(self):
        with tempfile.TemporaryDirectory() as d:
            repo, result = await self._edit_run(d)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.summary, "Fixed the greeting.")
            self.assertIn("1 file changed", result.diffstat)
            self.assertEqual(self.progress, ["Editing app.py"])
            self.assertEqual(result.landed_on, "main")
            self.assertEqual((repo / "app.py").read_text(), "print('fixed')\n")
            self.assertIn("claude: Fix the greeting", self._log(repo))
            self.assertEqual(self._branches(repo), ["main"])                 # task branch cleaned up
            self.assertFalse(Path(d, "wt", "repo", "sub-1-abc123").exists())  # ...and its worktree

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_commit_is_rebased_when_the_branch_moved_during_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            script = ("echo \"print('fixed')\" > app.py; echo notes > \"$REPO/notes.txt\"; "
                      "git -C \"$REPO\" add notes.txt; git -C \"$REPO\" -c user.name=t -c user.email=t@t commit -q -m mine")
            repo, result = await self._edit_run(d, script=script)
            self.assertEqual(result.landed_on, "main", result.hold_reason)
            self.assertEqual(self._log(repo).split("\n")[:2], ["claude: Fix the greeting", "mine"])
            self.assertEqual((repo / "app.py").read_text(), "print('fixed')\n")

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_uncommitted_work_in_the_same_file_holds_the_commit(self):
        with tempfile.TemporaryDirectory() as d:
            repo, result = await self._edit_run(d, before=lambda r: (r / "app.py").write_text("print('mine')\n"))
            self.assertIsNone(result.landed_on)
            self.assertIn("uncommitted changes in app.py", result.hold_reason)
            self.assertEqual((repo / "app.py").read_text(), "print('mine')\n")   # DIIZZY's work untouched
            self.assertIn("claude/sub-1-abc123", self._branches(repo))
            self.assertTrue(Path(result.worktree).is_dir())

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_uncommitted_work_elsewhere_doesnt_block_it(self):
        with tempfile.TemporaryDirectory() as d:
            def before(r):
                (r / "other.py").write_text("x = 1\n")
                subprocess.run(["git", "-C", str(r), "add", "other.py"], check=True)
            repo, result = await self._edit_run(d, before=before)
            self.assertEqual(result.landed_on, "main", result.hold_reason)
            self.assertEqual((repo / "other.py").read_text(), "x = 1\n")

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_conflicting_commit_on_the_branch_holds_it(self):
        with tempfile.TemporaryDirectory() as d:
            script = ("echo \"print('fixed')\" > app.py; echo \"print('theirs')\" > \"$REPO/app.py\"; "
                      "git -C \"$REPO\" -c user.name=t -c user.email=t@t commit -qam theirs")
            repo, result = await self._edit_run(d, script=script)
            self.assertIsNone(result.landed_on)
            self.assertIn("conflicts", result.hold_reason)
            self.assertEqual((repo / "app.py").read_text(), "print('theirs')\n")
            wt_status = subprocess.run(["git", "-C", result.worktree, "status", "--porcelain"], capture_output=True, text=True)
            self.assertEqual(wt_status.stdout.strip(), "")                     # rebase was aborted cleanly

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_reported_test_failures_hold_the_commit(self):
        with tempfile.TemporaryDirectory() as d:
            repo, result = await self._edit_run(d, result_text="Fixed it but test_x fails.\nTESTS FAILED")
            self.assertIsNone(result.landed_on)
            self.assertEqual(result.hold_reason, "Claude reported failing tests")
            self.assertEqual(result.summary, "Fixed it but test_x fails.")
            self.assertEqual((repo / "app.py").read_text(), "print('hi')\n")

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_landing_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as d:
            repo, result = await self._edit_run(d, env={"CLAUDE_EDIT_LAND": "branch"})
            self.assertIsNone(result.landed_on)
            self.assertEqual(result.branch, "claude/sub-1-abc123")
            self.assertEqual((repo / "app.py").read_text(), "print('hi')\n")
            self.assertIn("claude: Fix the greeting", self._log(repo, "claude/sub-1-abc123"))

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_no_changes_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as d:
            repo, result = await self._edit_run(d, script="true", result_text="Nothing to change.")
            self.assertIsNone(result.branch)
            self.assertEqual(self._branches(repo), ["main"])


class ShowVisualToolTests(unittest.IsolatedAsyncioTestCase):
    def test_beats_are_cleaned_up(self):
        clean = desktop_tools.normalize_beats([
            {"say": "Table first.", "lines": "1-2", "note": "x" * 300},
            {"say": "", "lines": [3]},                      # nothing to say: dropped
            {"say": "Fill it.", "lines": [2, 99, "4,5"]},   # out-of-range line dropped
            "garbage",
        ], n_lines=5)
        self.assertEqual([b["lines"] for b in clean], [[1, 2], [2, 4, 5]])
        self.assertEqual(len(clean[0]["note"]), 120)

    async def test_long_code_opens_in_the_browser(self):
        sent = []

        async def send(message):
            sent.append(message)
            session.resolve(message["id"], True, "Opened in his browser.")

        session = desktop_session.DesktopSession(send)
        desktop_session.set_active(session)
        self.addCleanup(desktop_session.clear_active, session)
        result = await desktop_tools.show_visual("Big", "\n".join(f"line {i}" for i in range(60)), "python",
                                                 [{"say": "Look.", "lines": [1]}])
        self.assertTrue(sent[0]["args"]["keep_open"])
        self.assertIn("1 beat(s)", result)


class ShowTableAndDiagramTests(unittest.IsolatedAsyncioTestCase):
    def test_table_beats_keep_only_cells_inside_the_grid(self):
        clean = desktop_tools.normalize_table_beats([
            {"say": "Base row.", "cells": [[0, 0], [9, 9]], "fill": [[0, 1, 7], [5, 5, 1], [0, 2]], "arrows": [[0, 0, 0, 1], [0, 0, 9, 9]]},
        ], rows=3, cols=3)
        self.assertEqual(clean[0]["cells"], [[0, 0]])
        self.assertEqual(clean[0]["fill"], [[0, 1, "7"]])
        self.assertEqual(clean[0]["arrows"], [[0, 0, 0, 1]])

    def test_bare_source_cells_point_at_the_filled_cell(self):
        clean = desktop_tools.normalize_table_beats(
            [{"say": "dp[1][1].", "cells": [[1, 1]], "fill": [[1, 1, "1"]], "arrows": [[0, 0], [0, 1], [1, 0]]}], rows=3, cols=3)
        self.assertEqual(clean[0]["arrows"], [[0, 0, 1, 1], [0, 1, 1, 1], [1, 0, 1, 1]])

    def test_dict_shaped_cells_and_fills_are_accepted(self):
        clean = desktop_tools.normalize_table_beats(
            [{"say": "x", "cells": [{"row": 0, "col": 1}], "fill": [{"row": 0, "col": 1, "value": 4}],
              "arrows": [{"from": [0, 0], "to": [0, 1]}]}], rows=2, cols=2)
        self.assertEqual((clean[0]["cells"], clean[0]["fill"], clean[0]["arrows"]), ([[0, 1]], [[0, 1, "4"]], [[0, 0, 0, 1]]))

    async def _capture(self):
        sent = []

        async def send(message):
            sent.append(message)
            session.resolve(message["id"], True, "On screen.")

        session = desktop_session.DesktopSession(send)
        desktop_session.set_active(session)
        self.addCleanup(desktop_session.clear_active, session)
        return sent

    async def test_show_table_sends_a_padded_grid(self):
        sent = await self._capture()
        await desktop_tools.show_table("Knapsack", 2, 3, ["a"], None, [["0", "1"]],
                                       [{"say": "Fill it.", "fill": [[1, 2, 9]]}])
        args = sent[0]["args"]
        self.assertEqual((args["kind"], args["cells"], args["row_labels"]), ("table", [["0", "1", ""], ["", "", ""]], ["a", ""]))
        self.assertEqual(args["beats"][0]["fill"], [[1, 2, "9"]])

    async def test_show_diagram_drops_dangling_edges(self):
        sent = await self._capture()
        await desktop_tools.show_diagram("Flow", [{"id": "a", "label": "A"}, {"id": "b"}],
                                         [{"from": "a", "to": "b"}, {"from": "a", "to": "zzz"}],
                                         [{"say": "Start.", "nodes": ["a", "zzz"], "edges": [["a", "b"]]}])
        args = sent[0]["args"]
        self.assertEqual([e["to"] for e in args["edges"]], ["b"])
        self.assertEqual(args["beats"][0]["nodes"], ["a"])


class CalendarReminderTests(unittest.TestCase):
    def setUp(self):
        calendar_watcher._reminded.clear()

    def test_reminds_once_inside_the_window_and_skips_all_day_and_past(self):
        now = datetime(2026, 10, 25, 21, 50, tzinfo=timezone.utc)
        soon = (now + timedelta(minutes=8)).isoformat()
        events = [
            {"id": "a", "summary": "Recruiter call", "start": soon, "location": "Zoom"},
            {"id": "b", "summary": "Later", "start": (now + timedelta(hours=2)).isoformat()},
            {"id": "c", "summary": "All day", "start": "2026-10-25"},
            {"id": "d", "summary": "Already started", "start": (now - timedelta(minutes=1)).isoformat()},
        ]
        due = calendar_watcher.due_reminders(events, now, minutes=10)
        self.assertEqual([(e["id"], m) for e, m in due], [("a", 8)])
        self.assertEqual(calendar_watcher.due_reminders(events, now, minutes=10), [])
        self.assertEqual(calendar_watcher.reminder_text(due[0][0], 8), "📅 Recruiter call starts in 8 min, at 9:58 pm (Zoom).")


class GoogleAuthAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_alerts_once_per_service_and_only_for_revoked_tokens(self):
        redis, sent, offered = FakeRedis(), [], []

        async def consider(data):
            offered.append(data)

        with mock.patch("backend.database.redis_connection.get_redis_client", lambda: redis), \
             mock.patch("backend.agent.operator_notify.send_operator_dm", side_effect=lambda t: sent.append((t, "dm"))), \
             mock.patch.object(desktop_notify.announcer, "consider", consider):
            await google_auth_health.report_google_auth_failure("Gmail", Exception("timeout"))
            await google_auth_health.report_google_auth_failure("Gmail", Exception("invalid_grant: Token has been expired or revoked."))
            await google_auth_health.report_google_auth_failure("Gmail", Exception("invalid_grant: again"))
        self.assertEqual(len(sent), 1)
        self.assertIn("generate_gmail_refresh_token", sent[0][0])
        self.assertEqual(sent[0][1], "dm")
        self.assertEqual(offered[0]["type"], "system_alert")

    async def test_new_kinds_pop_up(self):
        self.assertEqual(await desktop_notify.classify({"type": "calendar_soon"}), "calendar")
        self.assertEqual(await desktop_notify.classify({"type": "system_alert"}), "alert")


if __name__ == "__main__":
    unittest.main()
