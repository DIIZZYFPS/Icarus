"""Thought-bubble reporting: busy() records jobs in Redis, snapshot() lists and prunes them."""

import json
import time
import unittest
from unittest import mock

from backend.agent import busy as busy_mod
from backend.agent import processor


class FakeHashRedis:
    def __init__(self):
        self.h = {}

    async def hset(self, key, field, value):
        self.h[field] = value

    async def hgetall(self, key):
        return dict(self.h)

    async def hdel(self, key, *fields):
        for f in fields:
            self.h.pop(f, None)


class BusyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = FakeHashRedis()
        for p in (mock.patch.object(busy_mod, "_redis", lambda: self.redis),
                  mock.patch.object(busy_mod, "_enabled", lambda: True)):
            p.start()
            self.addCleanup(p.stop)

    async def test_job_is_listed_while_running_then_done(self):
        async with busy_mod.busy("discord"):
            items = await busy_mod.snapshot()
            self.assertEqual([(i["source"], i["done"]) for i in items], [("discord", False)])
        items = await busy_mod.snapshot()
        self.assertEqual([(i["source"], i["done"], i["ok"]) for i in items], [("discord", True, True)])

    async def test_failure_is_reported_and_reraised(self):
        with self.assertRaises(RuntimeError):
            async with busy_mod.busy("email"):
                raise RuntimeError("boom")
        [item] = await busy_mod.snapshot()
        self.assertEqual((item["done"], item["ok"]), (True, False))

    async def test_expired_and_garbage_entries_are_pruned(self):
        self.redis.h["dead"] = json.dumps({"source": "discord", "started": 1, "until": time.time() - 1})
        self.redis.h["junk"] = "not json"
        self.redis.h["odd"] = json.dumps({"source": "nope", "until": time.time() + 60})
        self.assertEqual(await busy_mod.snapshot(), [])
        self.assertEqual(self.redis.h, {})

    async def test_unknown_source_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            async with busy_mod.busy("weather"):
                pass

    async def test_redis_down_never_breaks_the_work(self):
        class Down:
            async def hset(self, *a):
                raise ConnectionError("no redis")
        with mock.patch.object(busy_mod, "_redis", lambda: Down()):
            async with busy_mod.busy("subagent"):
                done = True
        self.assertTrue(done)

    async def test_disabled_under_tests_by_default(self):
        mock.patch.stopall()
        with mock.patch.object(busy_mod, "_redis", lambda: self.redis):
            async with busy_mod.busy("discord"):
                pass
        self.assertEqual(self.redis.h, {})


class ProcessMessageBusyTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_platforms_are_reported_and_desktop_is_not(self):
        sources = []

        class Recorder:
            def __init__(self, source):
                sources.append(source)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        async def inner(platform, user_id, text, **kw):
            return f"{platform}:{text}"

        wrapped = processor._shows_busy(inner)
        with mock.patch.object(busy_mod, "busy", Recorder):
            self.assertEqual(await wrapped("discord", "u", "hi"), "discord:hi")
            await wrapped("telegram", "u", "hi")
            await wrapped("desktop", "u", "hi")
        self.assertEqual(sources, ["discord", "telegram"])


if __name__ == "__main__":
    unittest.main()
