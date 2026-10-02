import asyncio
import contextlib
import io
import json
import threading
import unittest
from unittest import mock

from teslabot import appscheduler, control, main, scheduler, tesla, utils
from teslabot.asyncthread import to_async
from teslabot.config import Config
from teslabot.env import Env
from teslabot.filestate import FileState
from teslabot.mqtt import MqttControl
from teslabot.slack import SlackControl
from teslabot.matrix import MatrixControl
from tests.test_tesla import FakeTesla


class Chat(control.Control):
    def __init__(self):
        super().__init__()
        self.messages = []
        self.fail = False
        self.started = asyncio.Event()
        self.stopped = False

    async def setup(self):
        self.started.set()

    async def run(self):
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped = True

    async def send_message(self, message_context, message):
        if self.fail:
            raise control.MessageSendError("offline")
        self.messages.append((message_context, message))


class MultiControlTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        self.a, self.b = Chat(), Chat()
        self.multi = control.MultiControl([self.a, self.b])
        cfg = Config("unused", {"common": {"storage": "local"},
                                 "tesla": {"email": "test@example.com"},
                                 "mqtt": {"host": "localhost"}})
        self.state = FileState("tmp/multi-control-test-state.ini")
        self.state.save_to_storage = mock.AsyncMock()
        self.env = Env(cfg, self.state)
        with mock.patch("teslabot.tesla.TeslaSession", FakeTesla):
            self.app = tesla.App(self.multi, self.env)
        self.app.tesla.vehicle_list = mock.Mock(return_value=[])
        self.app.tesla.close = mock.Mock()

    def test_selection_validation(self):
        self.assertEqual(control.parse_controls(" matrix ,slack,mqtt "), ["matrix", "slack", "mqtt"])
        for value in ("", "matrix,", ",slack", "slack,,mqtt", "slack,slack", "Matrix", "unknown"):
            with self.assertRaises(control.ConfigError):
                control.parse_controls(value)

    async def test_origin_roles_local_ping_and_fail_closed(self):
        for adapter in (self.a, self.b):
            context = control.CommandContext(True, adapter)
            await adapter.process_message(context, "!ping")
            self.assertEqual(adapter.messages[-1], (context.to_message_context(), "pong"))
            await adapter.process_message(context, "!unknown")
            self.assertEqual(adapter.messages[-1][1], "No such command")
        counts = [len(a.messages) for a in (self.a, self.b)]
        with self.assertRaises(control.MessageSendError):
            await self.multi.send_message(control.MessageContext(True, Chat()), "private")
        self.a.fail = True
        with self.assertRaises(control.MessageSendError):
            await self.multi.send_message(control.MessageContext(True, self.a), "private")
        self.assertEqual(counts, [len(a.messages) for a in (self.a, self.b)])

    async def test_broadcast_is_bounded_and_independent(self):
        self.a.send_message = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        await self.multi.send_message(control.MessageContext(False), "timer")
        self.assertEqual(self.b.messages[-1][1], "timer")
        self.assertIsNone(control.CommandContext(False, self.multi, scheduled=True).to_message_context().origin)

    async def test_never_ready_chat_does_not_delay_healthy_delivery(self):
        async def never_ready(*args):
            await asyncio.Event().wait()
        self.a.send_message = never_ready
        original_wait = asyncio.wait_for
        async def accelerated_wait(task, timeout):
            self.assertEqual(timeout, 10)
            return await original_wait(task, 0.02)
        with mock.patch("teslabot.control.asyncio.wait_for", side_effect=accelerated_wait):
            task = asyncio.create_task(self.multi.send_message(control.MessageContext(False), "notice"))
            await asyncio.sleep(0.005)
            self.assertEqual(self.b.messages[-1][1], "notice")
            self.assertFalse(task.done())
            await task

    async def test_persisted_bang_loaded_before_ingress_and_shared(self):
        self.state["control"] = {"require_bang": "False"}
        await self.app.initialize()
        self.assertFalse(self.a.require_bang)
        self.assertFalse(self.b.require_bang)
        await self.app._command_set_require_bang(control.CommandContext(False, self.a), True)
        self.assertTrue(self.a.require_bang and self.b.require_bang)
        self.assertEqual(self.state["control"]["require_bang"], "True")

    async def test_mqtt_only_preserves_timers(self):
        self.multi.run_scheduled_commands = False
        saved = '{"info":{"command":["info"]},"time":"2030-01-01T00:00:00"}'
        self.state["timers"] = {"1": saved}
        await self.app.initialize()
        await self.state.save()
        self.assertEqual(self.state["timers"]["1"], saved)
        self.assertIsNone(self.app._scheduler._scheduler._task)

    async def test_terminal_adapter_failure_stops_healthy_sibling(self):
        self.a.run = mock.AsyncMock(side_effect=RuntimeError("secret sentinel"))
        with self.assertRaises(RuntimeError):
            await self.multi.run()
        self.assertTrue(self.b.stopped)
        self.a.run = mock.AsyncMock()
        with self.assertRaises(control.ControlException):
            await self.multi.run()

    async def test_logout_drains_inflight_and_rejects_old_queue(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        self.app.authorized = self.app.tesla.authorized = True
        def blocking():
            entered.set()
            release.wait(2)
            calls.append("inflight")
        active = asyncio.create_task(self.app._retry_to_async(blocking))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        queued = asyncio.create_task(self.app._retry_to_async(lambda: calls.append("queued")))
        await asyncio.sleep(0)
        logout = asyncio.create_task(self.app._command_logout(control.CommandContext(True, self.a), ()))
        await asyncio.sleep(0)
        self.assertFalse(self.app.authorized)
        self.assertEqual(self.app.tesla.logout_calls, 0)
        release.set()
        await active
        with self.assertRaises(tesla.AppException):
            await queued
        await logout
        self.assertEqual(calls, ["inflight"])
        self.assertEqual(self.app.tesla.logout_calls, 1)

    async def test_serialization_and_thread_cancellation_drain(self):
        entered = threading.Event()
        release = threading.Event()
        second = mock.Mock()
        def blocking():
            entered.set()
            release.wait(2)
        active = asyncio.create_task(self.app._retry_to_async(blocking))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        active.cancel()
        queued = asyncio.create_task(self.app._retry_to_async(second))
        await asyncio.sleep(0.01)
        second.assert_not_called()
        self.assertFalse(active.done())
        active.cancel()
        await asyncio.sleep(0)
        second.assert_not_called()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await active
        await queued
        second.assert_called_once()

    async def test_chat_mqtt_and_timer_vehicle_paths_share_one_gate(self):
        active = 0
        maximum = 0
        guard = threading.Lock()
        def request(result):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            threading.Event().wait(0.005)
            with guard:
                active -= 1
            return result
        class Vehicle(dict):
            def sync_wake_up(self):
                return request(None)
            def command(self, *args, **kwargs):
                return request(True)
            def get_vehicle_data(self):
                return request({"climate_state": {}, "charge_state": {}})
        vehicle = Vehicle(display_name="Car", vin="VIN1", state="online", id_s="1")
        self.app.tesla.vehicle_list = lambda: request([vehicle])
        self.app.authorized = self.app.tesla.authorized = True
        await asyncio.gather(
            self.app.set_ac(None, True, control.CommandContext(False, self.a)),
            self.app.set_sauna(None, True, control.CommandContext(False, self.b)),
            self.app.refresh_vehicle(None, vehicle_id=self.app._vehicle_id(vehicle)),
            self.app._command_on_vehicle(control.CommandContext(False, self.multi, scheduled=True), None,
                                         lambda car: car.command("LOCK")),
        )
        self.assertEqual(maximum, 1)

    async def test_timer_notice_failure_does_not_prevent_command(self):
        import datetime
        sched = self.app._scheduler
        sched._commands = mock.Mock()
        sched._commands.invoke = mock.AsyncMock()
        self.multi.send_message = mock.AsyncMock(side_effect=[control.MessageSendError("offline"), None])
        async def unused():
            pass
        entry = scheduler.OneShot(unused, datetime.datetime.now(),
                                  appscheduler.SchedulerContext(appscheduler.AppTimerInfo(1, ["ac", "on"], None)))
        await sched._scheduler.add(entry)
        await sched._activate_timer(entry)
        sched._commands.invoke.assert_awaited_once()
        context = sched._commands.invoke.await_args.args[0]
        self.assertTrue(context.scheduled)
        self.assertIsNone(context.to_message_context().origin)

    async def test_override_reconciliation_and_persisted_restore(self):
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        self.app.authorized = self.app.tesla.authorized = True
        self.app.tesla.vehicle_list.return_value = [{"display_name": "One"}, {"display_name": "Two"}]
        await self.app._command_set_override_vehicles(control.CommandContext(False, self.a), ["Two"])
        self.assertTrue(mqtt._auth_event.is_set())
        self.assertEqual(self.app.cached_vehicle_list, [{"display_name": "Two"}])
        self.state["tesla"] = {"override_vehicles": "Two"}
        self.app.override_vehicles_lc = set()
        await self.app._load_state()
        self.assertEqual(self.app.override_vehicles_lc, {"two"})

    async def test_authorization_commit_precedes_response_failure(self):
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        vehicle = {"display_name": "Named car", "vin": "VIN1"}
        self.app.tesla.vehicle_list.return_value = [vehicle]
        self.a.fail = True
        with self.assertRaises(control.MessageSendError):
            await self.app._command_authorized(control.CommandContext(True, self.a), "https://example.com/callback?code=sentinel")
        self.assertTrue(self.app.authorized)
        self.assertTrue(mqtt._auth_event.is_set())
        self.app.tesla.vehicle_list.assert_called_once()
        self.assertEqual(self.app.cached_vehicle_list, [vehicle])
        await mqtt.close()
        self.assertEqual(self.app.auth_events, [])

    async def test_failed_auth_and_logout_fail_closed(self):
        self.app.tesla.fetch_token = mock.Mock(side_effect=RuntimeError("private callback"))
        with self.assertRaises(RuntimeError):
            await self.app._command_authorized(control.CommandContext(True, self.a), "https://example.com")
        self.assertFalse(self.app.authorized)
        self.app.authorized = self.app.tesla.authorized = True
        self.app.tesla.logout = mock.Mock(side_effect=RuntimeError("private token"))
        with self.assertRaises(RuntimeError):
            await self.app._command_logout(control.CommandContext(True, self.a), ())
        self.assertFalse(self.app.authorized)
        self.assertFalse(self.app._auth_transition)

    async def test_logout_during_token_exchange_then_reauthorize(self):
        entered, release = threading.Event(), threading.Event()
        original_fetch = self.app.tesla.fetch_token
        first = True
        def fetch(**kwargs):
            nonlocal first
            if first:
                first = False
                entered.set()
                release.wait(2)
            return original_fetch(**kwargs)
        self.app.tesla.fetch_token = fetch
        context = control.CommandContext(True, self.a)
        authorize = asyncio.create_task(self.app._command_authorized(context, "https://example.com/first"))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        logout = asyncio.create_task(self.app._command_logout(context, ()))
        await asyncio.sleep(0)
        reauthorize = asyncio.create_task(self.app._command_authorized(context, "https://example.com/second"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(authorize, logout, reauthorize)
        self.assertTrue(self.app.authorized)
        self.assertEqual(self.app.auth_generation, 3)
        self.assertEqual(self.app.tesla.logout_calls, 1)
        self.assertEqual(self.app.tesla.fetch_token_calls, ["https://example.com/first", "https://example.com/second"])

    async def test_parser_and_auth_exception_logs_preserve_diagnostics(self):
        import oauthlib.oauth2
        secret = "SECRET_SENTINEL"
        self.app.tesla.fetch_token = mock.Mock(side_effect=oauthlib.oauth2.InvalidGrantError(description=secret))
        output = io.StringIO()
        with self.assertLogs(level="DEBUG") as logs, contextlib.redirect_stdout(output):
            for text in (f"!AuThOrIzE https://example.com/callback?code={secret}",
                         f"!authorize not-a-url-{secret}", f"!authorize https://example.com/{secret} extra"):
                await self.a.process_message(control.CommandContext(True, self.a), text)
        logged = "\n".join(logs.output) + output.getvalue()
        self.assertIn(secret, logged)
        self.assertIn("https://example.com", logged)
        self.assertIn("Command:", logged)
        self.assertIn("Traceback (most recent call last)", logged)

    async def test_delay_helper_propagates_external_cancellation(self):
        report = mock.AsyncMock()
        task = asyncio.create_task(utils.call_with_delay_info(60, report, asyncio.sleep(60)))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        report.assert_not_awaited()

    async def test_slack_admission_and_admin_egress(self):
        slack = SlackControl.__new__(SlackControl)
        control.Control.__init__(slack)
        slack._channel_id = "normal"
        slack._admin_channel_id = "admin"
        slack.process_message = mock.AsyncMock()
        for event in ({"channel": "normal"}, {"channel": "admin"}, {"channel": "other"}, {},
                      {"channel": "admin", "bot_id": "bot"}, {"channel": "admin", "subtype": "bot_message"}):
            await slack._process_event(dict(event, text="!authorize private"))
        self.assertEqual(slack.process_message.await_count, 2)
        self.assertFalse(slack.process_message.await_args_list[0].args[0].admin_room)
        self.assertTrue(slack.process_message.await_args_list[1].args[0].admin_room)
        slack._client = mock.Mock()
        slack._client.api_call.side_effect = lambda **kwargs: asyncio.ensure_future(asyncio.sleep(0, result={"ok": True}))
        await slack.send_message(control.MessageContext(True, slack), "private URL")
        self.assertEqual(slack._client.api_call.call_args.kwargs["json"]["channel"], "admin")
        slack._admin_channel_id = ""
        with self.assertRaises(control.MessageSendError):
            await slack.send_message(control.MessageContext(True, slack), "private URL")

    async def test_new_chat_state_and_owned_slack_session(self):
        cfg = Config("unused", {"slack": {"slack_api_secret_id": "test-api", "slack_app_secret_id": "test-app",
                                          "slack_admin_channel_id": "admin", "channel": "#normal"},
                                 "matrix": {"store_path": "tmp/matrix-test-store", "homeserver": "https://example.com",
                                            "mxid": "@bot:example.com"}})
        env = Env(cfg, self.state)
        with mock.patch("teslabot.matrix.AsyncClient") as matrix_client:
            matrix_client.return_value.close = mock.AsyncMock()
            matrix = MatrixControl(env)
            self.assertIsNone(matrix._room_id)
            await matrix.close()
        session = mock.Mock()
        session.close = mock.AsyncMock()
        with mock.patch("teslabot.slack.aiohttp.ClientSession", return_value=session) as session_class, \
             mock.patch("teslabot.slack.WebClient") as web_client, mock.patch.dict("os.environ", {}, clear=True):
            slack = SlackControl(env)
            session_class.assert_not_called()
            slack._channel_id = "normal"
            await slack.setup()
            self.assertIs(web_client.call_args.kwargs["session"], session)
            await self.state.save()
            self.assertEqual(self.state["slack"]["channel_id"], "normal")
            await slack.close()
            session.close.assert_awaited_once()

    async def test_mqtt_manifest_restart_cleanup_before_online(self):
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        self.app.authorized = self.app.tesla.authorized = True
        old, new = "0123456789abcdef", "abcdef0123456789"
        self.state["mqtt_owned"] = {mqtt._manifest_key: json.dumps([old]), "unrelated": json.dumps([old])}
        self.app._get_vehicle_list = mock.AsyncMock(return_value=[{"display_name": "New"}])
        self.app._vehicle_id = mock.Mock(return_value=new)
        client = mock.AsyncMock()
        self.assertTrue(await mqtt._reconcile(client, 0))
        publications = client.publish.await_args_list
        online_index = next(i for i, call in enumerate(publications) if call.args[1] == "online")
        deleted = [call.args[0] for call in publications[:online_index] if call.args[1] == ""]
        self.assertIn(f"teslabot/{old}/state", deleted)
        self.assertEqual(sum(topic.endswith("/config") for topic in deleted), 8)
        self.assertEqual(json.loads(self.state["mqtt_owned"][mqtt._manifest_key]), [new])
        self.assertEqual(json.loads(self.state["mqtt_owned"]["unrelated"]), [old])
        self.app._auth_changed(False)
        client.publish.reset_mock()
        self.assertTrue(await mqtt._reconcile(client, 1))
        self.assertNotIn("online", [call.args[1] for call in client.publish.await_args_list])
        self.assertEqual(mqtt.vehicles, {})

    async def test_mqtt_superseded_enumeration_never_online(self):
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        self.app.authorized = True
        async def enumerate_vehicles():
            self.app._auth_changed(False)
            return []
        self.app._get_vehicle_list = enumerate_vehicles
        client = mock.AsyncMock()
        self.assertFalse(await mqtt._reconcile(client, 0))
        self.assertNotIn("online", [call.args[1] for call in client.publish.await_args_list])

    async def test_mqtt_old_generation_does_not_publish_action_result(self):
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        mqtt._generation = 0
        mqtt.vehicles = {"id1": "Car"}
        self.app.authorized = True
        async def action(*args, **kwargs):
            self.app._auth_changed(False)
            return tesla.ActionResult("id1", "ac", True, True)
        self.app.set_ac = action
        client = mock.AsyncMock()
        await mqtt._handle(client, "teslabot/id1/ac/set", "ON", False)
        client.publish.assert_not_awaited()

    async def test_matrix_child_failure_cancels_other_child_and_closes(self):
        matrix = MatrixControl.__new__(MatrixControl)
        matrix._send_tasks = {}
        matrix._delivery_lock = asyncio.Lock()
        matrix._close_task = None
        matrix._closed = False
        matrix._logged_in = True
        matrix._sync_token = None
        matrix._client = mock.Mock()
        matrix._client.synced = asyncio.Event()
        matrix._client.sync_forever = mock.AsyncMock(side_effect=RuntimeError("sync failed"))
        matrix._client.close = mock.AsyncMock()
        with self.assertRaises(RuntimeError):
            await matrix.run()
        await matrix.close()
        matrix._client.close.assert_awaited_once()
        self.assertFalse(any("after_first_sync" in str(task.get_coro()) for task in asyncio.all_tasks()))

    async def test_scheduler_shutdown_cancels_active_callback(self):
        sched = scheduler.Scheduler()
        started = asyncio.Event()
        async def callback():
            started.set()
            await asyncio.Event().wait()
        import datetime
        entry = scheduler.OneShot(callback, datetime.datetime.now() + datetime.timedelta(seconds=0.05), None)
        await sched.add(entry)
        await sched.start()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(sched.stop(), 1)
        self.assertIsNone(sched._task)

    async def test_main_mqtt_only_missing_auth_is_nonzero_and_closes(self):
        cfg = Config("unused", {"common": {"storage": "local", "control": "mqtt"},
                                 "tesla": {"email": "test@example.com"}, "mqtt": {"host": "localhost"}})
        mqtt = MqttControl(Env(cfg, self.state))
        mqtt.close = mock.AsyncMock()
        with mock.patch.object(main.log, "setup_logging"), \
             mock.patch.object(main.config, "get_args", return_value=mock.Mock(version=False)), \
             mock.patch.object(main.config, "Config", return_value=cfg), \
             mock.patch.object(main.filestate, "FileState", return_value=self.state), \
             mock.patch("teslabot.mqtt.MqttControl", return_value=mqtt), \
             mock.patch("teslabot.tesla.TeslaSession", FakeTesla), \
             self.assertLogs("teslabot.main", "ERROR") as logs:
            with self.assertRaises(SystemExit) as caught:
                await main.async_main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("MQTT-only startup requires cached", "\n".join(logs.output))
        mqtt.close.assert_awaited_once()

    async def test_main_partial_construction_failure_closes_prior_client(self):
        cfg = Config("unused", {"common": {"storage": "local", "control": "slack,mqtt"}})
        self.a.close = mock.AsyncMock()
        with mock.patch.object(main.log, "setup_logging"), \
             mock.patch.object(main.config, "get_args", return_value=mock.Mock(version=False)), \
             mock.patch.object(main.config, "Config", return_value=cfg), \
             mock.patch.object(main.filestate, "FileState", return_value=self.state), \
             mock.patch("teslabot.slack.SlackControl", return_value=self.a), \
             mock.patch("teslabot.mqtt.MqttControl", side_effect=control.ConfigError("MQTT configuration invalid")), \
             self.assertLogs("teslabot.main", "ERROR"):
            with self.assertRaises(SystemExit):
                await main.async_main()
        self.a.close.assert_awaited_once()

    async def test_observed_credential_failure_invalidates_generation(self):
        from oauthlib.oauth2 import InvalidGrantError
        self.app.authorized = True
        def failure():
            raise InvalidGrantError()
        with self.assertRaises(InvalidGrantError):
            await self.app._retry_to_async(failure)
        self.assertFalse(self.app.authorized)
        self.assertEqual(self.app.auth_generation, 1)

    async def test_custom_mqtt_namespaces_do_not_share_discovery(self):
        one = MqttControl(self.env)
        cfg = Config("unused", {"mqtt": {"host": "localhost", "prefix": "other/instance"}})
        two = MqttControl(Env(cfg, self.state))
        self.assertNotEqual(one._config_topic("0123456789abcdef", "switch/ac"),
                            two._config_topic("0123456789abcdef", "switch/ac"))
        self.assertNotEqual(one._discovery("0123456789abcdef", "Car")["switch/ac"]["unique_id"],
                            two._discovery("0123456789abcdef", "Car")["switch/ac"]["unique_id"])

    async def test_auth_while_broker_disconnected_and_clean_session_reconcile(self):
        import aiomqtt
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        retrying, reconnect, online = asyncio.Event(), asyncio.Event(), asyncio.Event()
        second_offline = asyncio.Event()
        clients = []
        class Client:
            def __init__(self, **kwargs):
                self.closed = False
                self.publications = []
                self.messages = self.receive()
                self.kwargs = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                self.closed = True
            async def publish(self, topic, payload, **kwargs):
                self.publications.append((topic, payload))
                if payload == "online":
                    online.set()
                if payload == "offline" and len(clients) >= 2:
                    second_offline.set()
            async def subscribe(self, *args, **kwargs):
                pass
            async def receive(self):
                await asyncio.Event().wait()
                yield None
        attempts = 0
        def connect(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise aiomqtt.MqttError("offline")
            client = Client(**kwargs)
            clients.append(client)
            return client
        async def retry_sleep(seconds):
            retrying.set()
            await reconnect.wait()
        with mock.patch("aiomqtt.Client", side_effect=connect), \
             mock.patch.object(mqtt, "_wait_retry", side_effect=retry_sleep):
            task = asyncio.create_task(mqtt.run())
            try:
                await asyncio.wait_for(retrying.wait(), 1)
                await asyncio.wait_for(self.app._command_authorized(control.CommandContext(True, self.a), "https://example.com/callback"), 1)
                self.assertTrue(self.app.authorized)
                reconnect.set()
                await asyncio.wait_for(online.wait(), 1)
                self.assertTrue(clients[0].kwargs["clean_session"])
                await self.app._command_logout(control.CommandContext(True, self.a), ())
                await asyncio.wait_for(second_offline.wait(), 1)
                self.assertTrue(clients[0].closed)
                self.assertGreaterEqual(len(clients), 2)
                self.assertNotIn("online", [payload for _, payload in clients[1].publications])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(all(client.closed for client in clients))
