import asyncio
import unittest
from unittest import mock
from typing import Dict, List, Tuple

from teslabot.config import Config
from teslabot.control import CommandContext, Control, MessageContext
from teslabot.env import Env
from teslabot.filestate import FileState
from teslabot import tesla


class FakeControl(Control):
    def __init__(self) -> None:
        super().__init__()
        self.messages: List[Tuple[MessageContext, str]] = []

    async def setup(self) -> None:
        pass

    async def send_message(self, message_context: MessageContext, message: str) -> None:
        self.messages.append((message_context, message))

    async def run(self) -> None:
        pass


class FakeTesla:
    def __init__(self, email: str, **kwargs: object) -> None:
        self.email = email
        self.kwargs: Dict[str, object] = kwargs
        self.authorized = False
        self.fetch_token_calls: List[str] = []
        self.logout_calls = 0

    def authorization_url(self) -> str:
        return "https://auth.tesla.com/oauth2/v3/authorize"

    def fetch_token(self, *, authorization_response: str) -> None:
        self.fetch_token_calls.append(authorization_response)
        self.authorized = True

    def logout(self) -> None:
        self.logout_calls += 1
        self.authorized = False


class TestTeslaAuthorization(unittest.TestCase):
    def setUp(self) -> None:
        self.tesla_class = mock.patch("teslabot.tesla.teslapy.Tesla", FakeTesla)
        self.tesla_class.start()
        self.addCleanup(self.tesla_class.stop)
        config = Config("test.ini", {
            "common": {"storage": "local"},
            "tesla": {
                "email": "driver@example.com",
                "credentials_store": "test-cache.json",
                "override_vehicles": "all",
            },
        })
        self.control = FakeControl()
        self.app = tesla.App(self.control, Env(config, FileState("test-state.ini")))
        self.admin_context = CommandContext(admin_room=True, control=self.control, txn="test")
        self.non_admin_context = CommandContext(admin_room=False, control=self.control, txn="test")

    def test_authorize_requires_admin_room_and_returns_url(self) -> None:
        asyncio.get_event_loop().run_until_complete(self.app._command_authorized(self.non_admin_context, None))

        self.assertEqual(self.control.messages[-1][1], "Please use the admin room for this command.")

        asyncio.get_event_loop().run_until_complete(self.app._command_authorized(self.admin_context, None))

        self.assertIn("https://auth.tesla.com/oauth2/v3/authorize", self.control.messages[-1][1])
        self.assertEqual(self.app.tesla.email, "driver@example.com")
        self.assertEqual(self.app.tesla.kwargs["cache_file"], "test-cache.json")

    def test_authorize_exchanges_callback_url_and_reports_success(self) -> None:
        callback_url = "https://auth.tesla.com/void/callback?code=secret"

        asyncio.get_event_loop().run_until_complete(self.app._command_authorized(self.admin_context, callback_url))

        self.assertEqual(self.app.tesla.fetch_token_calls, [callback_url])
        self.assertEqual(self.control.messages[-1][1], "Authorization successful")

    def test_logout_preserves_admin_only_behavior(self) -> None:
        self.app.tesla.authorized = True

        asyncio.get_event_loop().run_until_complete(self.app._command_logout(self.non_admin_context, ()))
        self.assertEqual(self.control.messages[-1][1], "Please use the admin room for this command.")
        self.assertEqual(self.app.tesla.logout_calls, 0)

        asyncio.get_event_loop().run_until_complete(self.app._command_logout(self.admin_context, ()))
        self.assertEqual(self.app.tesla.logout_calls, 1)
        self.assertEqual(self.control.messages[-1][1], "Logout successful!")
