"""Claude workspaces: a folder per area (a course), a scrubbed environment, and a read-only Canvas client."""

import asyncio
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from backend.agent import claude_runner, desktop_session, desktop_tools

CANVAS_PATH = Path(__file__).resolve().parents[1] / "tools" / "canvas"


def load_canvas():
    loader = importlib.machinery.SourceFileLoader("canvas_cli", str(CANVAS_PATH))
    spec = importlib.util.spec_from_loader("canvas_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class WorkspaceConfigTests(unittest.TestCase):
    def test_parses_paths_and_integrations(self):
        found = claude_runner.parse_workspaces("courses=~/Courses+canvas, notes=/tmp/n, junk")
        self.assertEqual(set(found), {"courses", "notes"})
        self.assertEqual(found["courses"].root, Path("~/Courses").expanduser())
        self.assertEqual(found["courses"].integrations, ("canvas",))
        self.assertEqual(found["notes"].integrations, ())

    def test_environment_is_scrubbed_to_the_basics_and_the_integration(self):
        ws = claude_runner.parse_workspaces("courses=/tmp/c+canvas")["courses"]
        source = {"PATH": "/usr/bin", "HOME": "/home/x", "LC_ALL": "C", "ANTHROPIC_API_KEY": "k",
                  "CANVAS_API_TOKEN": "t", "CANVAS_BASE_URL": "https://school.instructure.com",
                  "DISCORD_BOT_TOKEN": "d", "GITHUB_TOKEN": "g", "GOOGLE_API_KEY": "gk", "REDIS_URL": "r"}
        env = claude_runner.workspace_env(ws, source)
        self.assertEqual(env["CANVAS_API_TOKEN"], "t")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")
        for secret in ("DISCORD_BOT_TOKEN", "GITHUB_TOKEN", "GOOGLE_API_KEY", "REDIS_URL"):
            self.assertNotIn(secret, env)
        self.assertTrue(env["PATH"].startswith(str(claude_runner.TOOLS_DIR)))   # `canvas` resolves by name

    def test_no_tool_that_works_on_every_path_is_allowed(self):
        bare = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "Bash"}
        allowed = set(claude_runner.WORKSPACE_TOOLS) | {t for i in claude_runner.INTEGRATIONS.values() for t in i.tools}
        self.assertEqual(allowed & bare, set())
        self.assertIn("Read(./**)", allowed)        # reads are scoped to the area folder
        self.assertTrue(all(t.startswith("Bash(") and t.endswith(":*)") for t in allowed if t.startswith("Bash")))

    def test_canvas_variables_only_reach_workspaces_that_name_it(self):
        ws = claude_runner.parse_workspaces("notes=/tmp/n")["notes"]
        env = claude_runner.workspace_env(ws, {"PATH": "/usr/bin", "CANVAS_API_TOKEN": "t"})
        self.assertNotIn("CANVAS_API_TOKEN", env)


class RunWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    """A fake `claude` that records how it was started and writes a draft."""

    def fake_claude(self, d: Path, record: Path, task: str) -> Path:
        fake = d / "claude"
        fake.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""\
            import json, os, sys
            json.dump({{"cwd": os.getcwd(), "args": sys.argv[1:], "env": dict(os.environ)}}, open({str(record)!r}, "w"))
            os.makedirs("Lab8", exist_ok=True)
            open("Lab8/notes.md", "w").write("draft")
            if "EDIT" in sys.argv[2]:
                open("lab7.txt", "a").write(" tampered")
            if "DELETE" in sys.argv[2]:
                os.unlink("old.txt")
            print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Bash", "input": {{"command": "canvas get /api/v1/courses"}}}}]}}}}))
            print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "Pulled the assignment and drafted notes.", "num_turns": 3, "total_cost_usd": 0.02}}))
            """))
        fake.chmod(0o755)
        return fake

    async def run_it(self, d, *, area="CS4440", integrations="canvas", extra_env=None, task="Pull the week 3 assignment"):
        root = Path(d, "Courses")
        ws = claude_runner.parse_workspaces(f"courses={root}+{integrations}".rstrip("+"))["courses"]
        record = Path(d, "record.json")
        env = {"CLAUDE_BIN": str(self.fake_claude(Path(d), record, "sub-1-abc123")),
               "CANVAS_BASE_URL": "https://school.instructure.com", "CANVAS_API_TOKEN": "tok",
               "DISCORD_BOT_TOKEN": "secret", **(extra_env or {})}
        self.progress = []

        async def on_progress(text):
            self.progress.append(text)

        with mock.patch.dict(os.environ, env):
            result = await claude_runner.run_workspace(task_id="sub-1-abc123", workspace=ws, area=area,
                                                       task=task, on_progress=on_progress)
        return root, result, (json.loads(record.read_text()) if record.exists() else None)

    async def test_runs_in_the_area_folder_with_a_scrubbed_environment(self):
        with tempfile.TemporaryDirectory() as d:
            root, result, seen = await self.run_it(d)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(Path(seen["cwd"]).resolve(), (root / "CS4440").resolve())
            self.assertEqual(seen["env"]["CANVAS_API_TOKEN"], "tok")
            self.assertNotIn("DISCORD_BOT_TOKEN", seen["env"])
            self.assertTrue(seen["env"]["PATH"].startswith(str(claude_runner.TOOLS_DIR)))
            args = seen["args"]
            self.assertIn("Bash(canvas:*)", args)
            self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")
            note = args[args.index("--append-system-prompt") + 1]
            self.assertIn("Never overwrite, edit or delete", note)
            self.assertNotIn("icarus/", note)                                 # no per-task directory
            self.assertIn("read-only `canvas` command", note)
            self.assertNotIn("Bash(git", " ".join(args))                    # no git, no general shell

    async def test_reports_what_it_saved_and_progress(self):
        with tempfile.TemporaryDirectory() as d:
            root, result, _ = await self.run_it(d)
            self.assertEqual(result.summary, "Pulled the assignment and drafted notes.")
            self.assertEqual(result.files, ["Lab8/notes.md"])
            self.assertEqual(result.artifacts["folder"], str(root / "CS4440"))
            self.assertEqual((result.artifacts["changed"], result.artifacts["removed"]), ([], []))
            self.assertEqual(len(self.progress), 1)

    def seed(self, d):
        area = Path(d) / "Courses" / "CS4440"
        area.mkdir(parents=True)
        (area / "lab7.txt").write_text("my notes")
        (area / "old.txt").write_text("old")

    async def test_touching_an_existing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            self.seed(d)
            root, result, _ = await self.run_it(d, task="Do it. EDIT")
            self.assertEqual(result.artifacts["changed"], ["lab7.txt"])
            self.assertEqual(result.files, ["Lab8/notes.md"])                  # new files are still just the new ones

    async def test_deleting_an_existing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            self.seed(d)
            root, result, _ = await self.run_it(d, task="Do it. DELETE")
            self.assertEqual(result.artifacts["removed"], ["old.txt"])
            self.assertEqual(result.artifacts["changed"], [])

    async def test_leaving_existing_files_alone_reports_nothing_odd(self):
        with tempfile.TemporaryDirectory() as d:
            self.seed(d)
            root, result, _ = await self.run_it(d)
            self.assertEqual((result.artifacts["changed"], result.artifacts["removed"]), ([], []))
            self.assertTrue(result.artifacts["tracked"])

    def test_an_area_matches_an_existing_folder_ignoring_case(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CS4660").mkdir()
            (root / "Capstone").mkdir()
            self.assertEqual(claude_runner.resolve_area(root, "cs4660"), root / "CS4660")
            self.assertEqual(claude_runner.resolve_area(root, "CS4660"), root / "CS4660")
            self.assertEqual(claude_runner.resolve_area(root, "capstone"), root / "Capstone")
            self.assertEqual(claude_runner.resolve_area(root, "cs9999"), root / "cs9999")      # new: as given
            (root / "cs4660x").mkdir()
            (root / "Dup").mkdir()
            (root / "dup").mkdir() if not (root / "dup").exists() else None
            if (root / "dup").samefile(root / "Dup"):      # case-insensitive filesystem: nothing to disambiguate
                return
            self.assertEqual(claude_runner.resolve_area(root, "DUP"), root / "DUP")            # ambiguous: don't guess

    async def test_runs_in_the_existing_folder_when_only_the_case_differs(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Courses" / "CS4440").mkdir(parents=True)
            root, result, seen = await self.run_it(d, area="cs4440")
            self.assertTrue(result.ok, result.error)
            self.assertEqual(Path(seen["cwd"]).resolve(), (root / "CS4440").resolve())
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["CS4440"])               # no lowercase twin

    async def test_bad_area_names_never_touch_the_disk(self):
        with tempfile.TemporaryDirectory() as d:
            for area in ("../etc", "a/b", "", ".hidden", "x" * 80):
                root, result, seen = await self.run_it(d, area=area)
                self.assertFalse(result.ok, area)
                self.assertIsNone(seen, area)
            self.assertFalse((Path(d) / "Courses").exists())

    async def test_missing_token_fails_fast_and_names_the_variable(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"CANVAS_API_TOKEN": ""}):
                ws = claude_runner.parse_workspaces(f"courses={d}+canvas")["courses"]
                result = await claude_runner.run_workspace(task_id="sub-2-abc123", workspace=ws, area="CS1", task="x")
            self.assertFalse(result.ok)
            self.assertIn("CANVAS_API_TOKEN", result.error)
            self.assertNotIn("tok", result.error)


class SnapshotTests(unittest.TestCase):
    def test_diff_finds_new_changed_and_missing_files(self):
        before = {"a": (1, 1), "b": (2, 2), "c": (3, 3)}
        after = {"a": (1, 1), "b": (2, 9), "d": (4, 4)}
        self.assertEqual(claude_runner.diff_trees(before, after), (["d"], ["b"], ["c"]))

    def test_snapshot_skips_git_and_gives_up_on_huge_folders(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").mkdir()
            (root / ".git" / "x").write_text("x")
            (root / "sub").mkdir()
            (root / "sub" / "f.txt").write_text("f")
            self.assertEqual(sorted(claude_runner.snapshot_tree(root)), ["sub/f.txt"])
            with mock.patch.object(claude_runner, "SNAPSHOT_LIMIT", 0):
                self.assertIsNone(claude_runner.snapshot_tree(root))


class CanvasClientTests(unittest.TestCase):
    def setUp(self):
        self.canvas = load_canvas()
        patcher = mock.patch.dict(os.environ, {"CANVAS_BASE_URL": "https://school.instructure.com/", "CANVAS_API_TOKEN": "secret-token"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                self.canvas.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    class Resp:
        def __init__(self, body, link=None):
            self.body, self.headers = body, ({"Link": link} if link else {})

        def read(self, n=-1):
            data, self.body = self.body, b""
            return data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_only_api_paths_on_the_configured_host(self):
        for bad in ("/courses", "https://evil.example/api/v1/courses", "//evil.example/api/v1/x", "api/v1/courses"):
            code, out, err = self.run_main("get", bad)
            self.assertNotEqual(code, 0, bad)
            self.assertEqual(out, "")

    def test_get_sends_the_token_but_never_prints_it(self):
        seen = []

        def fake_request(url, token):
            seen.append((url, token))
            return self.Resp(b'[{"id": 1, "name": "CS4440"}]')
        with mock.patch.object(self.canvas, "request", fake_request):
            code, out, err = self.run_main("get", "/api/v1/courses?enrollment_state=active")
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("https://school.instructure.com/api/v1/courses?enrollment_state=active", "secret-token")])
        self.assertEqual(json.loads(out), [{"id": 1, "name": "CS4440"}])
        self.assertNotIn("secret-token", out + err)

    def test_all_follows_pages_and_merges_lists(self):
        pages = {
            "https://school.instructure.com/api/v1/x": self.Resp(b'[1, 2]', '<https://school.instructure.com/api/v1/x?page=2>; rel="next"'),
            "https://school.instructure.com/api/v1/x?page=2": self.Resp(b'[3]'),
        }
        with mock.patch.object(self.canvas, "request", lambda url, token: pages[url]):
            code, out, _ = self.run_main("get", "/api/v1/x", "--all")
        self.assertEqual((code, json.loads(out)), (0, [1, 2, 3]))

    def test_will_not_follow_a_page_link_to_another_host(self):
        first = self.Resp(b'[1]', '<https://evil.example/steal>; rel="next"')
        with mock.patch.object(self.canvas, "request", lambda url, token: first):
            code, out, err = self.run_main("get", "/api/v1/x", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("another host", err)

    def test_downloads_stay_on_the_host_and_inside_the_folder(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(self.canvas.os, "getcwd", lambda: d):
            with mock.patch.object(self.canvas, "request", lambda url, token: self.Resp(b"pdf-bytes")):
                code, out, _ = self.run_main("download", "https://school.instructure.com/files/9/download", "notes/w3.pdf")
                self.assertEqual(code, 0)
                self.assertEqual((Path(d) / "notes" / "w3.pdf").read_bytes(), b"pdf-bytes")
                self.assertNotEqual(self.run_main("download", "https://school.instructure.com/files/9/download", "../../escape")[0], 0)
                self.assertNotEqual(self.run_main("download", "https://evil.example/files/9", "x.pdf")[0], 0)
            self.assertFalse((Path(d).parent / "escape").exists())

    def test_nothing_but_get_and_download_exists(self):
        for argv in (("post", "/api/v1/x"), ("put", "/api/v1/x"), ("delete", "/api/v1/x"), ()):
            self.assertEqual(self.run_main(*argv)[0], 2)


class WorkspaceToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        async def send(message):
            pass

        self.session = desktop_session.DesktopSession(send)

        async def fake_call(name, args, timeout=None):
            self.calls.append(name)
            return {"ok": True, "result": "confirmed"}

        self.session.call = fake_call
        desktop_session.set_active(self.session)
        self.addCleanup(desktop_session.clear_active, self.session)
        self.published = []

        class Redis:
            async def publish(inner, channel, payload):
                self.published.append(json.loads(payload))
                return 1

        for p in (
            mock.patch.dict(os.environ, {"DESKTOP_CLAUDE_PROJECTS": "icarus=/x", "DESKTOP_CLAUDE_WORKSPACES": "courses=~/Courses+canvas"}),
            mock.patch("backend.database.redis_connection.get_redis_client", lambda: Redis()),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_a_workspace_task_needs_an_area_and_no_confirmation(self):
        reply = await desktop_tools.run_claude_task("courses", "Pull the week 3 assignment", area="CS4440")
        self.assertEqual(self.calls, [])                                   # read-only token, drafts only: nothing to confirm
        payload = self.published[-1]
        self.assertEqual((payload["project"], payload["mode"], payload["area"]), ("courses", "work", "CS4440"))
        self.assertIn("courses/CS4440", reply)

    async def test_mode_edit_cannot_be_used_to_reach_a_workspace_differently(self):
        await desktop_tools.run_claude_task("courses", "x", mode="edit", area="CS4440")
        self.assertEqual(self.published[-1]["mode"], "work")
        self.assertEqual(self.calls, [])

    async def test_missing_or_unsafe_area_is_refused(self):
        for area in ("", "../x", "a/b"):
            self.assertIn("needs an area", await desktop_tools.run_claude_task("courses", "x", area=area))
        self.assertEqual(self.published, [])

    async def test_projects_still_work_and_the_prompt_names_the_workspace(self):
        await desktop_tools.run_claude_task("icarus", "explain the build", "ask")
        self.assertEqual((self.published[-1]["project"], self.published[-1]["mode"]), ("icarus", "ask"))
        prompt = desktop_tools.desktop_prompt()
        self.assertIn("courses (a folder per area; can read Canvas)", prompt)
        self.assertIn("Projects: icarus.", prompt)


if __name__ == "__main__":
    unittest.main()
