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
    def test_existing_authorization_redaction_is_retained_in_ingress_log(self) -> None:
        control = FakeControl()
        context = CommandContext(admin_room=True, control=control, txn="test")
        callback_url = "https://auth.tesla.com/void/callback?code=secret"

        with self.assertLogs("teslabot.control", "INFO") as logs:
            asyncio.get_event_loop().run_until_complete(
                control.process_message(context, f"!authorize {callback_url}"))

        ingress = logs.records[0].getMessage()
        self.assertEqual(ingress, "< !authorize [redacted]")
        self.assertNotIn(callback_url, ingress)
