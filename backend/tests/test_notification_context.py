import unittest

from backend.agent import notification_repo


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.lists = {}
        self.expirations = {}

    async def exists(self, key):
        return int(key in self.hashes)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})

    async def hgetall(self, key):
        return self.hashes.get(key, {}).copy()

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    async def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    async def expire(self, key, seconds):
        self.expirations[key] = seconds


class NotificationContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = FakeRedis()
        self.original_get_redis = notification_repo.get_redis_client
        notification_repo.get_redis_client = lambda: self.redis

    async def asyncTearDown(self):
        notification_repo.get_redis_client = self.original_get_redis

    async def test_latest_pending_notification_is_used_for_unthreaded_dm_reply(self):
        scope = "discord:dm:42"
        first = await notification_repo.record_notification(
            conversation_id=scope,
            notification_type="email",
            source_id="mail-1",
            content="First notification",
        )
        second = await notification_repo.record_notification(
            conversation_id=scope,
            notification_type="job",
            source_id="job-1",
            content="Newest notification",
        )

        result = await notification_repo.get_pending_notification(conversation_id=scope)

        self.assertEqual(result["notification_id"], second)
        self.assertEqual(result["content"], "Newest notification")
        self.assertNotEqual(first, second)

    async def test_explicit_reply_requires_matching_delivery_message(self):
        scope = "discord:dm:42"
        notification_id = await notification_repo.record_notification(
            conversation_id=scope,
            notification_type="email",
            source_id="mail-2",
            content="Reply target",
        )
        await notification_repo.bind_delivery(
            notification_id,
            ["discord-message-8", "discord-message-9"],
        )

        self.assertIsNone(
            await notification_repo.get_pending_notification(
                conversation_id=scope,
                reply_to_message_id="discord-message-other",
            )
        )
        result = await notification_repo.get_pending_notification(
            conversation_id=scope,
            reply_to_message_id="discord-message-8",
        )
        self.assertEqual(result["content"], "Reply target")

        result = await notification_repo.get_pending_notification(
            conversation_id=scope,
            reply_to_message_id="discord-message-9",
        )
        self.assertEqual(result["content"], "Reply target")

    async def test_consumed_notification_is_not_reused(self):
        scope = "discord:dm:42"
        notification_id = await notification_repo.record_notification(
            conversation_id=scope,
            notification_type="email",
            source_id="mail-3",
            content="One shot",
        )
        await notification_repo.consume_notification(notification_id)
        self.assertIsNone(await notification_repo.get_pending_notification(conversation_id=scope))

    async def test_duplicate_record_does_not_reopen_consumed_notification(self):
        scope = "discord:dm:42"
        notification_id = await notification_repo.record_notification(
            conversation_id=scope,
            notification_type="email",
            source_id="mail-idempotent",
            content="Original content",
        )
        await notification_repo.bind_delivery(notification_id, ["discord-message-10"])
        await notification_repo.consume_notification(notification_id)

        duplicate_id = await notification_repo.record_notification(
            conversation_id=scope,
            notification_type="email",
            source_id="mail-idempotent",
            content="Retry content",
        )

        self.assertEqual(duplicate_id, notification_id)
        self.assertIsNone(await notification_repo.get_pending_notification(conversation_id=scope))
        stored = self.redis.hashes[notification_repo._record_key(notification_id)]
        self.assertEqual(stored["delivery_message_ids"], '["discord-message-10"]')
        self.assertTrue(stored["consumed_at"])

    async def test_other_scope_cannot_read_dm_notification(self):
        await notification_repo.record_notification(
            conversation_id="discord:dm:42",
            notification_type="email",
            source_id="mail-4",
            content="Private DM notification",
        )
        self.assertIsNone(
            await notification_repo.get_pending_notification(
                conversation_id="discord:server:777:channel:9001",
            )
        )


if __name__ == "__main__":
    unittest.main()
