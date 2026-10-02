import asyncio
import contextlib
import datetime
import types
import unittest
from unittest import mock
from typing import Optional

import aiohttp
import nio

from teslabot import appscheduler, control, scheduler, tesla
from teslabot.config import Config, ConfigException
from teslabot.env import Env
from teslabot.filestate import FileState
from teslabot.matrix import MatrixControl
import tests.test_multi_control as multi_tests
import tests.test_multi_control_review as review_tests
from teslabot.mqtt import MqttControl


class EncryptedNetwork:
    """Real nio room_send/share_group_session with fake crypto and HTTP I/O."""
    def __init__(self, client, sleep, scale):
        self.client = client
        self.sleep, self.scale = sleep, scale
        self.room_id = "!encrypted:example.com"
        self.calls = []
        self.active = set()
        self.claim_delay, self.share_delay, self.delivery_delay = 15, 35, 20
        self.claim_started = asyncio.Event()
        self.claim_block: Optional[asyncio.Event] = None
        self.share_started = asyncio.Event()
        self.share_block: Optional[asyncio.Event] = None
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.hold_cleanup = False
        self.fail_delivery = False
        client.access_token = "test-token"
        client.store = mock.Mock()
        client.olm = mock.Mock()
        self.session = types.SimpleNamespace(shared=False)
        client.olm.outbound_group_sessions = {self.room_id: self.session}
        client.olm.should_share_group_session.side_effect = lambda room: not self.session.shared
        client.olm.share_group_session_parallel.return_value = [
            ({("@user:example.com", "DEVICE")}, {"@user:example.com": {"DEVICE": {"ciphertext": "test-key"}}})
        ]
        room = nio.MatrixRoom(self.room_id, "@bot:example.com", encrypted=True)
        room.members_synced = True
        room.users["@user:example.com"] = mock.Mock()
        client.rooms[self.room_id] = room
        client.get_missing_sessions = mock.Mock(return_value={"@user:example.com": ["DEVICE"]})
        client.encrypt = mock.Mock(return_value=("m.room.encrypted", {"ciphertext": "encrypted-notice"}))
        client._send = self.send

    async def send(self, response_type, method, path, data=None, response_data=None, **kwargs):
        self.calls.append((response_type, method, path, data))
        task = asyncio.current_task()
        self.active.add(task)
        cancelled = False
        try:
            if response_type is nio.KeysClaimResponse:
                self.claim_started.set()
                if self.claim_block is not None:
                    await self.claim_block.wait()
                else:
                    await self.sleep(self.claim_delay * self.scale)
                return nio.KeysClaimResponse({}, {})
            if response_type is nio.ShareGroupSessionResponse:
                self.share_started.set()
                if self.share_block is not None:
                    await self.share_block.wait()
                else:
                    await self.sleep(self.share_delay * self.scale)
                assert response_data is not None
                return nio.ShareGroupSessionResponse(*response_data)
            if response_type is nio.RoomSendResponse:
                if self.fail_delivery:
                    raise aiohttp.ClientConnectionError("delivery connection detail")
                await self.sleep(self.delivery_delay * self.scale)
                return nio.RoomSendResponse("$delivered", self.room_id)
            raise AssertionError(f"Unexpected SDK request {response_type}")
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if cancelled and self.hold_cleanup:
                self.cleanup_started.set()
                await self.cleanup_release.wait()
            self.active.remove(task)


class MatrixTimeoutTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        self.tasks = []
        self.real_sleep, self.real_wait_for = asyncio.sleep, asyncio.wait_for
        self.scale = 0.002
        self.budgets = []
        self.matrix, self.env = self.build_control()
        self.network = EncryptedNetwork(self.matrix._client, self.real_sleep, self.scale)
        self.matrix._room_id = self.matrix._admin_room_id = self.network.room_id

    def build_control(self, settings=None):
        section = {"homeserver": "https://example.com", "mxid": "@bot:example.com",
                   "store_path": "tmp/matrix-timeout-store"}
        section.update(settings or {})
        cfg = Config("unused", {"matrix": section, "mqtt": {"host": "localhost"}})
        state = FileState("tmp/matrix-timeout-state.ini")
        state.save_to_storage = mock.AsyncMock()
        env = Env(cfg, state)
        return MatrixControl(env), env

    async def asyncTearDown(self):
        self.network.cleanup_release.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.matrix.close()

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    @contextlib.contextmanager
    def scaled_deadlines(self):
        original_wait = asyncio.wait
        def scaled(timeout):
            self.assertIn(timeout, (None, 10, self.matrix.readiness_timeout, self.matrix.send_timeout))
            self.budgets.append(timeout)
            return None if timeout is None else timeout * self.scale
        async def wait_for(awaitable, timeout):
            # Check real configured budgets, then enforce them with the real
            # asyncio timeout implementation on a uniformly scaled timeline.
            return await self.real_wait_for(awaitable, scaled(timeout))
        async def wait(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            return await original_wait(tasks, timeout=None if timeout is None else scaled(timeout), return_when=return_when)
        with mock.patch("teslabot.control.asyncio.wait_for", side_effect=wait_for), \
             mock.patch("teslabot.matrix.asyncio.wait", side_effect=wait):
            yield

    def assert_drained(self):
        self.assertEqual(self.matrix._send_tasks, {})
        self.assertEqual(self.matrix._client.sharing_session, {})
        self.assertEqual(self.network.active, set())

    async def delayed_ready(self, seconds):
        await self.real_sleep(seconds * self.scale)
        self.matrix._init_done.set()

    async def test_positive_finite_configuration_and_defaults(self):
        self.assertEqual((self.matrix.readiness_timeout, self.matrix.send_timeout), (120, 120))
        configured, _ = self.build_control({"readiness_timeout": "0.25", "send_timeout": "300"})
        self.assertEqual((configured.readiness_timeout, configured.send_timeout), (0.25, 300))
        self.assertIsNone(configured.message_timeout)
        await configured.close()
        for key in ("readiness_timeout", "send_timeout"):
            for value in ("0", "-1", "nan", "NaN", "inf", "-inf", "1e309", "invalid", "", " "):
                with self.subTest(key=key, value=value), mock.patch("teslabot.matrix.AsyncClient") as client:
                    with self.assertRaises((control.ConfigError, ConfigException)) as caught:
                        self.build_control({key: value})
                    self.assertIn(f"matrix.{key}", str(caught.exception))
                    client.assert_not_called()

    async def test_slow_ready_and_real_encrypted_send_succeed_for_all_routes(self):
        for route in ("local", "interactive", "broadcast"):
            with self.subTest(route=route):
                self.matrix._init_done.clear()
                self.network.session.shared = False
                self.budgets.clear()
                self.network.calls.clear()
                self.start(self.delayed_ready(70))
                context = control.CommandContext(False, self.matrix)
                with self.scaled_deadlines():
                    if route == "local":
                        await self.matrix.process_message(context, "!ping")
                        self.assertEqual(self.budgets, [120, 120])
                    else:
                        multi = control.MultiControl([self.matrix])
                        message_context = context.to_message_context() if route == "interactive" else control.MessageContext(False)
                        await multi.send_message(message_context, "slow encrypted message")
                        self.assertEqual(self.budgets, [None, 120, 120])
                # Both readiness and encrypted I/O exceed the former 10s limit;
                # their combined 140s also exceeds either single 120s budget.
                self.assertEqual([call[0] for call in self.network.calls],
                                 [nio.KeysClaimResponse, nio.ShareGroupSessionResponse, nio.RoomSendResponse])
                self.assertFalse(self.matrix._client.olm.share_group_session_parallel.call_args.kwargs["ignore_unverified_devices"])
                self.assert_drained()

    async def test_custom_phase_budgets_are_not_shortened_by_composite(self):
        await self.matrix.close()
        self.matrix, self.env = self.build_control({"readiness_timeout": "30", "send_timeout": "50"})
        self.network = EncryptedNetwork(self.matrix._client, self.real_sleep, self.scale)
        self.matrix._room_id = self.matrix._admin_room_id = self.network.room_id
        self.network.claim_delay, self.network.share_delay, self.network.delivery_delay = 10, 20, 10
        self.start(self.delayed_ready(20))
        with self.scaled_deadlines():
            await control.MultiControl([self.matrix]).send_message(control.MessageContext(True, self.matrix), "custom-budget message")
        self.assertEqual(self.budgets, [None, 30, 50])
        self.assert_drained()

    async def test_readiness_deadline_drops_before_send_and_next_send_works(self):
        self.matrix.readiness_timeout = 20
        with self.scaled_deadlines(), self.assertLogs("teslabot.matrix", "WARNING") as logs:
            with self.assertRaisesRegex(control.MessageSendError, "readiness timed out after 20"):
                await self.matrix.send_message(control.MessageContext(False), "readiness diagnostic payload")
        self.assertEqual(self.budgets, [20])
        self.assertEqual(self.network.calls, [])
        self.assertIn("readiness diagnostic payload", "\n".join(logs.output))
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assert_drained()
        self.matrix._init_done.set()
        with self.scaled_deadlines():
            await self.matrix.send_message(control.MessageContext(False), "after readiness timeout")
        self.assert_drained()

    async def test_delivery_deadline_drains_real_nio_share_then_next_send_works(self):
        self.matrix._init_done.set()
        self.matrix.send_timeout = 40
        self.network.share_block = asyncio.Event()
        with self.scaled_deadlines(), self.assertLogs("teslabot.matrix", "WARNING") as logs:
            with self.assertRaisesRegex(control.MessageSendError, "delivery timed out after 40"):
                await self.matrix.send_message(control.MessageContext(False), "sharing diagnostic payload")
        self.assertEqual(self.budgets, [120, 40])
        self.assertTrue(self.network.share_started.is_set())
        self.assertFalse(any(call[0] is nio.RoomSendResponse for call in self.network.calls))
        self.assertIn("sharing diagnostic payload", "\n".join(logs.output))
        self.assertIn(self.network.room_id, "\n".join(logs.output))
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assert_drained()
        self.network.share_block = None
        self.network.claim_delay = self.network.share_delay = self.network.delivery_delay = 0
        with self.scaled_deadlines():
            await self.matrix.send_message(control.MessageContext(False), "after sharing timeout")
        self.assertEqual(sum(call[0] is nio.RoomSendResponse for call in self.network.calls), 1)
        self.assert_drained()

    async def test_network_failure_preserves_detail_and_does_not_auto_retry(self):
        self.matrix._init_done.set()
        self.network.claim_delay = self.network.share_delay = self.network.delivery_delay = 0
        self.network.fail_delivery = True
        with self.scaled_deadlines():
            with self.assertRaisesRegex(control.MessageSendError, "delivery connection detail") as caught:
                await self.matrix.send_message(control.MessageContext(False), "failed message")
        self.assertIsInstance(caught.exception.__cause__, aiohttp.ClientConnectionError)
        self.assertEqual(sum(call[0] is nio.RoomSendResponse for call in self.network.calls), 1)
        self.assert_drained()
        self.network.fail_delivery = False
        with self.scaled_deadlines():
            await self.matrix.send_message(control.MessageContext(False), "next explicit send")
        self.assertEqual(sum(call[0] is nio.RoomSendResponse for call in self.network.calls), 2)
        self.assert_drained()

    async def test_caller_cancellation_reaches_both_pending_phases(self):
        for phase in ("readiness", "sharing"):
            with self.subTest(phase=phase), self.scaled_deadlines():
                self.budgets.clear()
                if phase == "sharing":
                    self.matrix._init_done.set()
                    self.network.claim_delay = 0
                    self.network.share_block = asyncio.Event()
                caller = self.start(self.matrix.send_message(control.MessageContext(False), "cancelled message"))
                if phase == "sharing":
                    await self.real_wait_for(self.network.share_started.wait(), 1)
                else:
                    while not self.budgets:
                        await self.real_sleep(0)
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                self.assert_drained()
        self.network.share_block = None
        self.network.share_delay = self.network.delivery_delay = 0
        with self.scaled_deadlines():
            await self.matrix.send_message(control.MessageContext(False), "after cancellation")
        self.assert_drained()

    async def test_close_cancels_pending_readiness_and_rejects_new_sends(self):
        with self.scaled_deadlines():
            caller = self.start(self.matrix.send_message(control.MessageContext(False), "closing before sync"))
            while not self.budgets:
                await self.real_sleep(0)
            await self.matrix.close()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            with self.assertRaisesRegex(control.MessageSendError, "closed"):
                await self.matrix.send_message(control.MessageContext(False), "after close")
        self.assertEqual(self.network.calls, [])
        self.assert_drained()

    async def test_close_drains_nio_sharing_tasks_before_client_close(self):
        self.matrix._init_done.set()
        self.network.claim_delay = 0
        self.network.share_block = asyncio.Event()
        self.network.hold_cleanup = True
        client_close = mock.AsyncMock(wraps=self.matrix._client.close)
        self.matrix._client.close = client_close
        with self.scaled_deadlines():
            caller = self.start(self.matrix.send_message(control.MessageContext(False), "closing while sharing"))
            await self.real_wait_for(self.network.share_started.wait(), 1)
            closing = self.start(self.matrix.close())
            await self.real_wait_for(self.network.cleanup_started.wait(), 1)
            self.assertFalse(closing.done())
            self.assertTrue(self.network.active)
            self.assertIn(self.network.room_id, self.matrix._client.sharing_session)
            client_close.assert_not_awaited()
            self.network.cleanup_release.set()
            await self.real_wait_for(closing, 1)
            with self.assertRaises(asyncio.CancelledError):
                await caller
            client_close.assert_awaited_once()
            self.assert_drained()

    async def test_healthy_notifications_ingress_mqtt_and_timer_continue(self):
        healthy = multi_tests.Chat()
        notified = asyncio.Event()
        original_send = healthy.send_message
        async def send(message_context, message):
            await original_send(message_context, message)
            notified.set()
        healthy.send_message = send
        mqtt = MqttControl(self.env)
        app = mock.Mock(auth_events=[], auth_generation=0, authorized=True)
        identity = "0123456789abcdef"
        app._get_vehicle_list = mock.AsyncMock(return_value=[{"display_name": "Test car"}])
        app._vehicle_id.return_value = identity
        app.set_ac = mock.AsyncMock(return_value=tesla.ActionResult(identity, "ac", True, False, "test rejection"))
        mqtt.set_app(app)
        online = asyncio.Event()
        result = asyncio.Event()
        class Broker(review_tests.BrokerClient):
            def __init__(self):
                self.incoming = asyncio.Queue()
                super().__init__(online)
            async def receive(self):
                while True:
                    yield await self.incoming.get()
            async def publish(self, topic, payload, **kwargs):
                await super().publish(topic, payload, **kwargs)
                if topic.endswith("/result"):
                    result.set()
        broker = Broker()
        self.matrix.setup = mock.AsyncMock()
        async def run_matrix():
            await asyncio.Event().wait()
        self.matrix.run = run_matrix
        self.network.share_block = asyncio.Event()
        multi = control.MultiControl([self.matrix, healthy, mqtt])
        with self.scaled_deadlines(), mock.patch("aiomqtt.Client", return_value=broker), \
             self.assertLogs("teslabot.control", "WARNING") as logs:
            runtime = self.start(multi.run())
            await self.real_wait_for(online.wait(), 1)
            broadcast = self.start(multi.send_message(control.MessageContext(False), "independent broadcast payload"))
            await self.real_wait_for(notified.wait(), 1)
            self.assertFalse(broadcast.done())
            for phase in ("readiness", "delivery"):
                if phase == "delivery":
                    self.matrix._init_done.set()
                    await self.real_wait_for(self.network.share_started.wait(), 1)
                await healthy.process_message(control.CommandContext(False, healthy), "!ping")
                self.assertEqual(healthy.messages[-1][1], "pong")
                result.clear()
                await broker.incoming.put(types.SimpleNamespace(topic=f"teslabot/{identity}/ac/set", payload=b"ON", retain=False))
                await self.real_wait_for(result.wait(), 1)
                self.assertFalse(broadcast.done())
                self.assertFalse(runtime.done())
            await self.real_wait_for(broadcast, 1)
            self.assertEqual(app.set_ac.await_count, 2)
            self.assertIn("independent broadcast payload", "\n".join(logs.output))
            self.assertIn("Traceback", "\n".join(logs.output))
            self.assert_drained()
            # A scheduled operation proceeds after the finite notification bound.
            self.matrix.send_timeout = 20
            self.network.claim_delay = 0
            sched = appscheduler.AppScheduler([], self.env.state, multi)
            sched._commands = mock.Mock(invoke=mock.AsyncMock())
            entry = scheduler.OneShot(mock.AsyncMock(), datetime.datetime.now(),
                                      appscheduler.SchedulerContext(appscheduler.AppTimerInfo(1, ["ac", "on"], None)))
            await sched._scheduler.add(entry)
            await self.real_wait_for(sched._activate_timer(entry), 1)
            sched._commands.invoke.assert_awaited_once()
            self.assertFalse(runtime.done())
            self.assert_drained()
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runtime
        await mqtt.close()

    async def test_other_controls_retain_finite_ten_second_bound(self):
        stalled = multi_tests.Chat()
        stopped = asyncio.Event()
        async def never_send(message_context, message):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        stalled.send_message = never_send
        healthy = multi_tests.Chat()
        with self.scaled_deadlines(), self.assertLogs("teslabot.control", "WARNING"):
            await control.MultiControl([stalled, healthy]).send_message(control.MessageContext(False), "bounded broadcast")
        self.assertEqual(self.budgets, [10, 10])
        self.assertTrue(stopped.is_set())
        self.assertEqual(healthy.messages[-1][1], "bounded broadcast")
        self.budgets.clear()
        with self.scaled_deadlines():
            with self.assertRaises(asyncio.TimeoutError):
                await control.MultiControl([stalled]).send_message(control.MessageContext(False, stalled), "bounded reply")
        self.assertEqual(self.budgets, [10])

    async def test_caller_cancel_then_close_does_not_recancel_sdk_cleanup(self):
        self.matrix._init_done.set()
        self.network.claim_delay = 0
        self.network.share_block = asyncio.Event()
        self.network.hold_cleanup = True
        client_close = mock.AsyncMock(wraps=self.matrix._client.close)
        self.matrix._client.close = client_close
        with self.scaled_deadlines():
            caller = self.start(self.matrix.send_message(control.MessageContext(False), "cancel then close"))
            await self.real_wait_for(self.network.share_started.wait(), 1)
            caller.cancel()
            await self.real_wait_for(self.network.cleanup_started.wait(), 1)
            closing = self.start(self.matrix.close())
            await self.real_sleep(0.01)
            self.assertFalse(closing.done())
            self.assertFalse(caller.done())
            self.assertTrue(self.network.active)
            client_close.assert_not_awaited()
            self.network.cleanup_release.set()
            await self.real_wait_for(closing, 1)
            with self.assertRaises(asyncio.CancelledError):
                await caller
        client_close.assert_awaited_once()
        self.assert_drained()

    async def test_repeated_caller_and_concurrent_close_cancellation_drain_once(self):
        self.matrix._init_done.set()
        self.network.claim_delay = 0
        self.network.share_block = asyncio.Event()
        self.network.hold_cleanup = True
        client_close = mock.AsyncMock(wraps=self.matrix._client.close)
        self.matrix._client.close = client_close
        with self.scaled_deadlines():
            caller = self.start(self.matrix.send_message(control.MessageContext(False), "repeated cancellation"))
            await self.real_wait_for(self.network.share_started.wait(), 1)
            caller.cancel()
            await self.real_wait_for(self.network.cleanup_started.wait(), 1)
            first_close = self.start(self.matrix.close())
            second_close = self.start(self.matrix.close())
            await self.real_sleep(0)
            for _ in range(3):
                caller.cancel()
                first_close.cancel()
                await self.real_sleep(0)
            self.assertFalse(caller.done())
            self.assertFalse(first_close.done())
            self.assertFalse(second_close.done())
            self.assertTrue(self.network.active)
            client_close.assert_not_awaited()
            self.network.cleanup_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await self.real_wait_for(caller, 1)
            with self.assertRaises(asyncio.CancelledError):
                await self.real_wait_for(first_close, 1)
            await self.real_wait_for(second_close, 1)
        client_close.assert_awaited_once()
        self.assert_drained()

    async def test_close_during_delivery_deadline_cleanup_does_not_recancel(self):
        self.matrix._init_done.set()
        self.matrix.send_timeout = 20
        self.network.claim_delay = 0
        self.network.share_block = asyncio.Event()
        self.network.hold_cleanup = True
        client_close = mock.AsyncMock(wraps=self.matrix._client.close)
        self.matrix._client.close = client_close
        with self.scaled_deadlines():
            caller = self.start(self.matrix.send_message(control.MessageContext(False), "deadline then close"))
            await self.real_wait_for(self.network.cleanup_started.wait(), 1)
            closing = self.start(self.matrix.close())
            await self.real_sleep(0.01)
            self.assertFalse(closing.done())
            self.assertTrue(self.network.active)
            client_close.assert_not_awaited()
            self.network.cleanup_release.set()
            await self.real_wait_for(closing, 1)
            with self.assertRaises(asyncio.CancelledError):
                await caller
        client_close.assert_awaited_once()
        self.assert_drained()

    async def test_key_claim_timeout_recovers_for_next_explicit_send(self):
        self.matrix._init_done.set()
        self.matrix.send_timeout = 20
        self.network.claim_block = asyncio.Event()
        original_missing = self.matrix._client.get_missing_sessions
        with self.scaled_deadlines(), self.assertLogs("teslabot.matrix", "WARNING") as logs:
            with self.assertRaisesRegex(control.MessageSendError, "delivery timed out"):
                await self.matrix.send_message(control.MessageContext(False), "interrupted key claim payload")
        self.assertTrue(self.network.claim_started.is_set())
        self.assertFalse(self.network.share_started.is_set())
        self.assertIs(self.matrix._client.get_missing_sessions, original_missing)
        self.assertIn("interrupted key claim payload", "\n".join(logs.output))
        self.assertIn(self.network.room_id, "\n".join(logs.output))
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assert_drained()
        self.network.claim_block = None
        self.network.claim_delay = self.network.share_delay = self.network.delivery_delay = 0
        with self.scaled_deadlines():
            await self.matrix.send_message(control.MessageContext(False), "next explicit after claim timeout")
        self.assertEqual([call[0] for call in self.network.calls],
                         [nio.KeysClaimResponse, nio.KeysClaimResponse, nio.ShareGroupSessionResponse, nio.RoomSendResponse])
        self.assertFalse(self.matrix._client.olm.share_group_session_parallel.call_args.kwargs["ignore_unverified_devices"])
        self.assert_drained()

    async def test_key_claim_cancellation_with_concurrent_same_room_send(self):
        self.matrix._init_done.set()
        self.network.claim_block = asyncio.Event()
        self.network.hold_cleanup = True
        with self.scaled_deadlines(), self.assertLogs("teslabot.matrix", "WARNING"):
            first = self.start(self.matrix.send_message(control.MessageContext(False), "cancelled first claim"))
            await self.real_wait_for(self.network.claim_started.wait(), 1)
            event = self.matrix._client.sharing_session[self.network.room_id]
            second = self.start(self.matrix.send_message(control.MessageContext(False), "queued same-room send"))
            await self.real_sleep(0.01)
            self.assertEqual(len(self.network.calls), 1)
            first.cancel()
            await self.real_wait_for(self.network.cleanup_started.wait(), 1)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.assertIs(self.matrix._client.sharing_session[self.network.room_id], event)
            self.network.claim_block = None
            self.network.claim_delay = self.network.share_delay = self.network.delivery_delay = 0
            self.network.cleanup_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await self.real_wait_for(first, 1)
            await self.real_wait_for(second, 1)
        self.assertTrue(event.is_set())
        self.assertEqual([call[0] for call in self.network.calls],
                         [nio.KeysClaimResponse, nio.KeysClaimResponse, nio.ShareGroupSessionResponse, nio.RoomSendResponse])
        self.assertFalse(self.matrix._client.olm.share_group_session_parallel.call_args.kwargs["ignore_unverified_devices"])
        self.assert_drained()

    async def test_queued_send_timeout_does_not_remove_active_claim_owner_event(self):
        self.matrix._init_done.set()
        self.network.claim_block = asyncio.Event()
        with self.scaled_deadlines(), self.assertLogs("teslabot.matrix", "WARNING"):
            first = self.start(self.matrix.send_message(control.MessageContext(False), "active owner"))
            await self.real_wait_for(self.network.claim_started.wait(), 1)
            event = self.matrix._client.sharing_session[self.network.room_id]
            # The first phase already captured its 120s budget; only the queued
            # operation gets this shorter admission/delivery budget.
            self.matrix.send_timeout = 20
            with self.assertRaisesRegex(control.MessageSendError, "delivery timed out"):
                await self.matrix.send_message(control.MessageContext(False), "queued timeout")
            self.assertIs(self.matrix._client.sharing_session[self.network.room_id], event)
            self.assertFalse(event.is_set())
            self.assertFalse(first.done())
            self.assertEqual(len(self.network.calls), 1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        self.assertTrue(event.is_set())
        self.assert_drained()

    async def test_foreign_sharing_event_is_not_deleted_or_signalled(self):
        self.matrix._init_done.set()
        self.matrix.send_timeout = 20
        foreign = asyncio.Event()
        self.matrix._client.sharing_session[self.network.room_id] = foreign
        with self.scaled_deadlines(), self.assertLogs("teslabot.matrix", "WARNING"):
            with self.assertRaisesRegex(control.MessageSendError, "delivery timed out"):
                await self.matrix.send_message(control.MessageContext(False), "foreign sharing owner")
        self.assertIs(self.matrix._client.sharing_session[self.network.room_id], foreign)
        self.assertFalse(foreign.is_set())
        self.assertEqual(self.network.calls, [])
        self.assertEqual(self.matrix._send_tasks, {})
        # Complete that foreign owner's lifecycle explicitly, not via adapter
        # cleanup, then demonstrate that the adapter remains usable.
        self.matrix._client.sharing_session.pop(self.network.room_id)
        foreign.set()
        self.network.session.shared = True
        self.network.delivery_delay = 0
        with self.scaled_deadlines():
            await self.matrix.send_message(control.MessageContext(False), "after foreign owner completes")
        self.assert_drained()

    async def test_replaced_sharing_event_is_not_deleted_or_signalled(self):
        self.matrix._init_done.set()
        self.network.claim_block = asyncio.Event()
        foreign = asyncio.Event()
        with self.scaled_deadlines(), self.assertLogs("teslabot.matrix", "WARNING"):
            caller = self.start(self.matrix.send_message(control.MessageContext(False), "owned event replaced"))
            await self.real_wait_for(self.network.claim_started.wait(), 1)
            owned = self.matrix._client.sharing_session[self.network.room_id]
            self.matrix._client.sharing_session[self.network.room_id] = foreign
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
        self.assertTrue(owned.is_set())
        self.assertIs(self.matrix._client.sharing_session[self.network.room_id], foreign)
        self.assertFalse(foreign.is_set())
        self.assertEqual(self.matrix._send_tasks, {})
        self.assertEqual(self.network.active, set())
        self.matrix._client.sharing_session.pop(self.network.room_id)
        foreign.set()
