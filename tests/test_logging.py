import asyncio
import contextlib
import io
import logging
import unittest
from unittest import mock

from teslabot import control, log
from teslabot.matrix import MatrixControl
from teslabot.slack import SlackControl
from tests.test_multi_control import Chat


class TestLoggingSetup(unittest.TestCase):
    def test_setup_preserves_sdk_levels_handlers_and_propagation(self):
        root = logging.getLogger()
        app = logging.getLogger("teslabot")
        handlers, app_level = list(root.handlers), app.level
        names = ("teslapy", "requests_oauthlib", "oauthlib", "slack", "nio", "aiohttp", "urllib3")
        previous = {name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers),
                           logging.getLogger(name).propagate) for name in names}
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                log.setup_logging()
                for name in names:
                    logger = logging.getLogger(name)
                    self.assertEqual((logger.level, logger.handlers, logger.propagate), previous[name])
                logging.getLogger("teslapy").warning("SDK vehicle payload: VIN123, battery 72%")
                logging.getLogger("teslabot.mqtt").info("MQTT ready")
            self.assertIn("SDK vehicle payload: VIN123, battery 72%", output.getvalue())
            self.assertIn("MQTT ready", output.getvalue())
        finally:
            app.setLevel(app_level)
            for handler in root.handlers[:]:
                if handler not in handlers:
                    root.removeHandler(handler)
                    handler.close()


class TestDiagnosticDetails(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    async def test_command_arguments_and_failed_notification_remain_available(self):
        chat = Chat()
        with self.assertLogs("teslabot", "DEBUG") as logs:
            await chat.process_message(control.CommandContext(False, chat, txn="diagnostic-txn"), "!ping unexpected-argument")
        text = "\n".join(logs.output)
        self.assertIn("unexpected-argument", text)
        self.assertIn("diagnostic-txn", text)
        self.assertIn("Command: ['ping', 'unexpected-argument']", text)
        self.assertIn("Traceback (most recent call last)", text)
        chat.send_message = mock.AsyncMock(side_effect=control.MessageSendError("broker-response-detail"))
        multi = control.MultiControl([chat])
        with self.assertLogs("teslabot.control", "WARNING") as logs:
            await multi.send_message(control.MessageContext(True), "notification-payload")
        text = "\n".join(logs.output)
        self.assertIn("notification-payload", text)
        self.assertIn("broker-response-detail", text)
        self.assertIn("Traceback (most recent call last)", text)

    async def test_adapter_cleanup_keeps_exception_message_and_traceback(self):
        chat = Chat()
        chat.close = mock.AsyncMock(side_effect=RuntimeError("cleanup-response-detail"))
        with self.assertLogs("teslabot.control", "ERROR") as logs:
            await control.MultiControl([chat]).close()
        text = "\n".join(logs.output)
        self.assertIn("cleanup-response-detail", text)
        self.assertIn("Traceback (most recent call last)", text)

    async def test_chat_outgoing_payloads_remain_available(self):
        matrix = MatrixControl.__new__(MatrixControl)
        matrix._send_tasks = {}
        matrix._delivery_lock = asyncio.Lock()
        matrix._close_task = None
        matrix._closed = False
        matrix._admin_room_id = "admin-room-id"
        matrix.wait_ready = mock.AsyncMock()
        matrix._client = mock.Mock(room_send=mock.AsyncMock(return_value=mock.Mock(event_id="event")))
        slack = SlackControl.__new__(SlackControl)
        slack._admin_channel_id = "admin-channel-id"
        future = asyncio.get_running_loop().create_future()
        future.set_result({"ok": True})
        slack._client = mock.Mock(api_call=mock.Mock(return_value=future))
        with self.assertLogs("teslabot", "INFO") as logs:
            await matrix.send_message(control.MessageContext(True), "matrix-response-payload")
            await slack.send_message(control.MessageContext(True), "slack-response-payload")
        text = "\n".join(logs.output)
        self.assertIn("matrix-response-payload", text)
        self.assertIn("slack-response-payload", text)
        self.assertIn("admin-channel-id", text)
