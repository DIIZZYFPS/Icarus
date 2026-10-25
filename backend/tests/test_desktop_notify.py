"""Desktop pop-ups: which notifications speak up, bundling, and reply linking."""

import json
import time
import unittest
from unittest import mock

from backend.agent import desktop_notify, desktop_session, operator_notify
from backend.agent.subagent_resume import index_key, pause_key, resolve_paused_reply


class FakeRedis:
    def __init__(self):
        self.hashes, self.lists = {}, {}

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    async def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]


def _iso(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


class ClassifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rows = {}

        async def row(task_id):
            return self.rows.get(task_id)

        patcher = mock.patch.object(desktop_notify, "_task_row", row)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_paused_task_question_always_pops_up(self):
        self.assertEqual(await desktop_notify.classify({"type": "pause_question", "task_id": "sub-1"}), "pause_question")

    async def test_email_needs_critical_or_high_with_action(self):
        c = desktop_notify.classify
        self.assertEqual(await c({"type": "email_notification", "urgency": "critical"}), "email")
        self.assertEqual(await c({"type": "email_notification", "urgency": "high", "action_needed": True}), "email")
        self.assertIsNone(await c({"type": "email_notification", "urgency": "high", "action_needed": False}))
        self.assertIsNone(await c({"type": "email_notification", "urgency": "medium", "action_needed": True}))

    async def test_delegation_pops_up_when_recent_failed_or_from_the_desktop(self):
        c = desktop_notify.classify
        self.rows = {"new": {"status": "completed", "created_at": _iso(600)},
                     "old": {"status": "completed", "created_at": _iso(5 * 3600)},
                     "bad": {"status": "failed", "created_at": _iso(5 * 3600)}}
        self.assertEqual(await c({"type": "delegation", "task_id": "new", "platform": "discord"}), "complete")
        self.assertIsNone(await c({"type": "delegation", "task_id": "old", "platform": "discord"}))
        self.assertEqual(await c({"type": "delegation", "task_id": "bad", "platform": "discord"}), "failed")
        self.assertEqual(await c({"type": "delegation", "task_id": "old", "platform": "desktop"}), "complete")

    async def test_escalations_pop_up_and_housekeeping_stays_quiet(self):
        c = desktop_notify.classify
        self.assertEqual(await c({"type": "escalation", "message": "PR #31 opened"}), "complete")
        self.assertEqual(await c({"type": "escalation", "message": "Escalation failed: tests broke"}), "failed")
        for kind in ("subagent_report", "subagent_lifecycle", "consultation", "deploy_check", "job_notification"):
            self.assertIsNone(await c({"type": kind, "message": "x"}), kind)


class AnnouncerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pushed = []

        async def send(message):
            self.pushed.append(message)

        self.session = desktop_session.DesktopSession(send)
        desktop_session.set_active(self.session)
        self.addCleanup(desktop_session.clear_active, self.session)
        self.redis = FakeRedis()
        self.prompts = []

        async def fake_process(platform, user_id, text, chat_id="0", **kw):
            self.prompts.append((platform, chat_id, kw.get("skip_history"), text))
            return "Two things. The playlist research is done, and a task needs your call on scopes."

        self.recorded = []

        async def record(**kw):
            self.recorded.append(kw)
            return "nid-1"

        async def bind(nid, ids):
            self.bound = (nid, ids)

        for p in (
            mock.patch("backend.agent.processor.process_message", fake_process),
            mock.patch("backend.agent.notification_repo.record_notification", record),
            mock.patch("backend.agent.notification_repo.bind_delivery", bind),
            mock.patch("backend.database.redis_connection.get_redis_client", lambda: self.redis),
            mock.patch.object(desktop_notify, "BUNDLE_SECONDS", .01),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_bundles_and_links_a_paused_task_to_the_spoken_reply(self):
        self.redis.hashes[pause_key("sub-9")] = {"delivery_message_ids": json.dumps(["discord-msg-1"])}
        await desktop_notify.announcer.announce([
            ("complete", {"type": "delegation", "task_id": "sub-8", "message": "Report: playlists need playlist-read-private"}),
            ("pause_question", {"type": "pause_question", "task_id": "sub-9", "question": "Private playlists too?"}),
        ])
        event = self.pushed[-1]
        self.assertEqual(event["type"], "notify")
        self.assertEqual(event["stamp"]["word"], "INPUT NEEDED")      # the most important item wins
        self.assertIn("2 UPDATES", event["stamp"]["sub"])
        platform, chat_id, skip_history, prompt = self.prompts[-1]
        self.assertEqual((platform, chat_id, skip_history), ("desktop", "desktop", True))
        self.assertIn("Private playlists too?", prompt)
        self.assertEqual(self.bound, ("nid-1", [event["id"]]))
        # a spoken reply carrying this pop-up's id resumes the paused task
        resolved = await resolve_paused_reply(self.redis, platform="desktop", chat_id="desktop",
                                              text="Yes, private ones too", reply_to_message_id=event["id"])
        self.assertEqual(resolved, ("sub-9", "Yes, private ones too"))

    async def test_nothing_happens_without_a_connected_daemon(self):
        desktop_session.clear_active(self.session)
        await desktop_notify.announcer.consider({"type": "pause_question", "task_id": "sub-1", "question": "?"})
        self.assertEqual(desktop_notify.announcer.pending, [])


class OperatorDmTests(unittest.TestCase):
    def test_no_dm_channel_means_nothing_is_sent_to_the_server(self):
        with mock.patch.object(operator_notify, "operator_dm_channel", return_value=None), \
             mock.patch.object(operator_notify, "send_discord") as send:
            result = operator_notify.send_operator_dm("Gmail access expired")
        send.assert_not_called()
        self.assertFalse(result.delivered)

    def test_dm_channel_is_opened_once_even_when_alerts_race(self):
        import threading
        operator_notify._dm_channel_cache.clear()
        opened = []

        def slow_open(token, operator):
            opened.append(operator)
            return "dm-1"

        with mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "t", "DISCORD_OPERATOR_ID": "42"}), \
             mock.patch.object(operator_notify, "_open_dm", slow_open):
            results = []
            threads = [threading.Thread(target=lambda: results.append(operator_notify.operator_dm_channel())) for _ in range(4)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual(results, ["dm-1"] * 4)
        self.assertEqual(opened, ["42"])
        operator_notify._dm_channel_cache.clear()


class DesktopOriginRoutingTests(unittest.TestCase):
    def test_councilor_messages_for_desktop_tasks_go_to_the_operator_dm(self):
        with mock.patch.object(operator_notify, "send_operator_dm", return_value="sent") as send, \
             mock.patch.object(operator_notify, "send_telegram") as telegram:
            self.assertEqual(operator_notify.notify("desktop", "desktop", "task paused"), "sent")
        send.assert_called_once_with("task paused")
        telegram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
