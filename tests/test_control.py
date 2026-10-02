import asyncio
import unittest

from teslabot.control import CommandContext, Control, MessageContext


class FakeControl(Control):
    async def setup(self) -> None:
        pass

    async def send_message(self, message_context: MessageContext, message: str) -> None:
        pass

    async def run(self) -> None:
        pass


class TestControlAuthorizationLogging(unittest.TestCase):
    def test_authorization_callback_is_redacted_from_logs(self) -> None:
        control = FakeControl()
        context = CommandContext(admin_room=True, control=control, txn="test")
        callback_url = "https://auth.tesla.com/void/callback?code=secret"

        with self.assertLogs("teslabot.control", "INFO") as logs:
            asyncio.get_event_loop().run_until_complete(
                control.process_message(context, f"!authorize {callback_url}"))

        self.assertIn("Command received", "\n".join(logs.output))
        self.assertNotIn(callback_url, "\n".join(logs.output))
