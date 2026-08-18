import unittest

from backend.agent.conversation_context import (
    build_conversation_id,
    is_server_conversation,
)


class ConversationScopeTests(unittest.TestCase):
    def test_discord_dm_scope_is_private_to_user(self):
        self.assertEqual(
            build_conversation_id(
                platform="discord",
                user_id="42",
                chat_id="9001",
            ),
            "discord:dm:42",
        )

    def test_discord_server_scope_contains_guild_and_channel(self):
        self.assertEqual(
            build_conversation_id(
                platform="discord",
                user_id="42",
                chat_id="9001",
                guild_id="777",
            ),
            "discord:server:777:channel:9001",
        )

    def test_server_scope_does_not_depend_on_author(self):
        first = build_conversation_id(
            platform="discord",
            user_id="42",
            chat_id="9001",
            guild_id="777",
        )
        second = build_conversation_id(
            platform="discord",
            user_id="99",
            chat_id="9001",
            guild_id="777",
        )
        self.assertEqual(first, second)

    def test_dm_and_server_scopes_can_never_collide(self):
        dm = build_conversation_id(
            platform="discord",
            user_id="42",
            chat_id="9001",
        )
        server = build_conversation_id(
            platform="discord",
            user_id="42",
            chat_id="9001",
            guild_id="777",
        )
        self.assertNotEqual(dm, server)
        self.assertFalse(is_server_conversation(dm))
        self.assertTrue(is_server_conversation(server))

    def test_telegram_scope_is_chat_scoped(self):
        self.assertEqual(
            build_conversation_id(
                platform="telegram",
                user_id="42",
                chat_id="9001",
            ),
            "telegram:chat:9001",
        )


if __name__ == "__main__":
    unittest.main()
