import asyncio
import contextlib
import datetime
import io
import json
import logging
import threading
import unittest
import urllib.parse
from unittest import mock
from typing import Any, AsyncIterator, Callable, Coroutine, TypeVar, cast

import aiomqtt
import nio
import requests
import requests.adapters
import oauthlib.oauth2
import slack.errors

from teslabot import appscheduler, config, control, main, scheduler, tesla
from teslabot.config import Config
from teslabot.env import Env
from teslabot.filestate import FileState
from teslabot.matrix import MatrixControl
from teslabot.mqtt import MqttControl
from teslabot.slack import SlackControl
from tests.test_multi_control import Chat
from tests.test_tesla import FakeTesla

T = TypeVar("T")


class HttpAdapter(requests.adapters.BaseAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, Any]] = []
        self.block = False
        self.entered = threading.Event()
        self.active = False

    def send(self, request: requests.PreparedRequest, stream: bool = False, timeout: Any = None,
             verify: Any = True, cert: Any = None, proxies: Any = None) -> requests.Response:
        self.calls.append((request.method, timeout))
        if self.block:
            self.active = True
            self.entered.set()
            try:
                if timeout is None:
                    raise AssertionError("Missing HTTP timeout")
                assert isinstance(timeout, (int, float))
                threading.Event().wait(timeout)
                raise requests.Timeout("SECRET_SENTINEL")
            finally:
                self.active = False
        response = requests.Response()
        response.status_code = 200
        response.url = request.url or ""
        response.request = request
        response._content = (b'{"access_token":"SECRET_SENTINEL","refresh_token":"SECRET_SENTINEL","token_type":"Bearer","expires_in":3600}'
                             if request.method == "POST" else b'{}')
        return response

    def close(self) -> None:
        pass


class BrokerClient:
    def __init__(self, online: asyncio.Event | None = None) -> None:
        self.publications: list[tuple[str, Any]] = []
        self.closed = False
        self.online = online
        self.messages = self.receive()

    async def __aenter__(self) -> "BrokerClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True

    async def publish(self, topic: str, payload: Any, **kwargs: Any) -> None:
        self.publications.append((topic, payload))
        if self.online is not None and payload == "online":
            self.online.set()

    async def subscribe(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def receive(self) -> AsyncIterator[Any]:
        await asyncio.Event().wait()
        yield None


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self) -> None:
        self.chat = Chat()
        self.multi = control.MultiControl([self.chat])
        self.cfg = Config("unused", {
            "common": {"storage": "local"}, "tesla": {"email": "test@example.com"},
            "mqtt": {"host": "localhost", "action_refresh_delay": "0"},
            "slack": {"slack_api_secret_id": "test", "slack_app_secret_id": "test",
                      "slack_admin_channel_id": "admin", "channel": "#normal"},
        })
        self.state = FileState("tmp/review-regression-state.ini")
        storage = mock.patch.object(self.state, "save_to_storage", new=mock.AsyncMock())
        storage.start()
        self.addCleanup(storage.stop)
        self.env = Env(self.cfg, self.state)
        with mock.patch("teslabot.tesla.TeslaSession", FakeTesla):
            self.app = tesla.App(self.multi, self.env)
        self.tasks: list[asyncio.Task[Any]] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.app.close()

    def start(self, awaitable: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    def sdk(self, timeout: float = 30) -> tuple[tesla.TeslaSession, HttpAdapter]:
        sdk = tesla.TeslaSession("test@example.com", timeout=timeout,
                                 cache_loader=lambda: {}, cache_dumper=lambda value: None)
        adapter = HttpAdapter()
        sdk.mount("https://", adapter)
        vehicles = mock.patch.object(sdk, "vehicle_list", return_value=[])
        vehicles.start()
        self.addCleanup(vehicles.stop)
        self.app.tesla = sdk
        return sdk, adapter

    @staticmethod
    def callback(url: str) -> str:
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        return "https://example.com/callback?code=SECRET_SENTINEL&state=" + state

    async def test_real_sdk_bounds_auth_and_refresh_http_requests(self) -> None:
        sdk, adapter = self.sdk()
        context = control.CommandContext(True, self.chat)
        await self.app._command_authorized(context, None)
        message = self.chat.messages[-1][1]
        url = message.split("Authorization URL: ", 1)[1].split(" ", 1)[0]
        await self.app._command_authorized(context, self.callback(url))
        await self.app._retry_to_async(sdk.refresh_token)
        self.assertEqual(adapter.calls, [("GET", 30), ("POST", 30), ("POST", 30)])
        self.assertTrue(self.app.authorized)

    async def test_sso_timeout_cancellation_drains_before_logout_and_close(self) -> None:
        sdk, adapter = self.sdk(timeout=0.05)
        url = sdk.authorization_url()
        adapter.block = True
        context = control.CommandContext(True, self.chat)
        authorize = self.start(self.app._command_authorized(context, self.callback(url)))
        while not adapter.entered.is_set():
            await asyncio.sleep(0.001)
        original_logout, original_close = sdk.logout, sdk.close
        def logout() -> Any:
            self.assertFalse(adapter.active)
            return original_logout()
        def close() -> Any:
            self.assertFalse(adapter.active)
            return original_close()
        logout_patch = mock.patch.object(sdk, "logout", side_effect=logout)
        logout_mock = logout_patch.start()
        self.addCleanup(logout_patch.stop)
        close_patch = mock.patch.object(sdk, "close", side_effect=close)
        close_patch.start()
        self.addCleanup(close_patch.stop)
        authorize.cancel()
        logout_task = self.start(self.app._command_logout(context, ()))
        await asyncio.sleep(0)
        logout_mock.assert_not_called()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(authorize, 1)
        await asyncio.wait_for(logout_task, 1)
        await asyncio.wait_for(self.app.close(), 1)
        self.assertFalse(adapter.active)
        self.assertFalse(self.app.authorized)
        self.assertEqual(adapter.calls[-1], ("POST", 0.05))

    async def test_sso_timeout_is_operational_and_does_not_prevent_logout(self) -> None:
        sdk, adapter = self.sdk(timeout=0.02)
        callback = self.callback(sdk.authorization_url())
        adapter.block = True
        context = control.CommandContext(True, self.chat)
        with self.assertLogs(level="WARNING") as logs:
            await asyncio.wait_for(self.chat.process_message(context, "!authorize " + callback), 1)
        self.assertFalse(adapter.active)
        self.assertFalse(self.app.authorized)
        self.assertIn("Tesla request failed", self.chat.messages[-1][1])
        self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
        self.assertIn("Traceback (most recent call last)", "\n".join(logs.output))
        await asyncio.wait_for(self.app._command_logout(context, ()), 1)

    async def test_rejected_real_sdk_token_starts_fresh_pkce_flow(self) -> None:
        sdk, _ = self.sdk()
        sdk.token = {"access_token": "SECRET_SENTINEL", "token_type": "Bearer"}
        self.app.authorized = True
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        def rejected() -> None:
            raise oauthlib.oauth2.InvalidGrantError()
        with self.assertRaises(oauthlib.oauth2.InvalidGrantError):
            await self.app._retry_to_async(rejected)
        self.assertTrue(sdk.authorized)
        self.assertFalse(self.app.authorized)
        context = control.CommandContext(True, self.chat)
        await self.app._command_authorized(context, None)
        self.assertFalse(sdk.authorized)
        self.assertFalse(mqtt._current())
        message = self.chat.messages[-1][1]
        self.assertNotIn("URL: None", message)
        url = message.split("Authorization URL: ", 1)[1].split(" ", 1)[0]
        self.assertTrue(sdk.code_verifier)
        await self.app._command_authorized(context, self.callback(url))
        self.assertTrue(await mqtt._reconcile(mock.AsyncMock(), self.app.auth_generation))
        self.assertTrue(mqtt._current())
        await mqtt.close()

    async def test_failed_logout_recovery_is_actionable_and_never_none_url(self) -> None:
        sdk, _ = self.sdk()
        sdk.token = {"access_token": "SECRET_SENTINEL", "token_type": "Bearer"}
        self.app.authorized = True
        logout = mock.patch.object(sdk, "logout", side_effect=OSError("SECRET_SENTINEL"))
        logout.start()
        self.addCleanup(logout.stop)
        context = control.CommandContext(True, self.chat)
        with self.assertRaises(OSError):
            await self.app._command_logout(context, ())
        self.assertFalse(self.app.authorized)
        with self.assertLogs(level="WARNING") as logs:
            await self.chat.process_message(context, "!authorize")
        self.assertIn("check credential storage/connectivity", self.chat.messages[-1][1])
        self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
        logout.stop()
        await self.app._command_authorized(context, None)
        url = self.chat.messages[-1][1].split("Authorization URL: ", 1)[1].split(" ", 1)[0]
        await self.app._command_authorized(context, self.callback(url))
        self.assertTrue(self.app.authorized)

    async def _reply_failure_case(self, kind: str) -> None:
        adapter: SlackControl | MatrixControl
        ingress: Callable[[str], Coroutine[Any, Any, None]]
        sender: mock.Mock
        if kind == "slack":
            adapter = SlackControl.__new__(SlackControl)
            control.Control.__init__(adapter)
            adapter._channel_id, adapter._admin_channel_id = "normal", "admin"
            adapter._aiohttp_session = None
            adapter._client = mock.Mock()
            recovered = False
            def send(**kwargs: Any) -> asyncio.Future[dict[str, Any]]:
                future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                if recovered:
                    future.set_result({"ok": True})
                else:
                    future.set_exception(slack.errors.SlackApiError("SECRET_SENTINEL", {"ok": False, "error": "delivery_failed"}))
                return future
            adapter._client.api_call = mock.Mock(side_effect=send)
            sender = adapter._client.api_call
            async def slack_ingress(text: str) -> None:
                assert isinstance(adapter, SlackControl)
                await adapter._process_event({"channel": "normal", "text": text})
            ingress = slack_ingress
        else:
            adapter = MatrixControl.__new__(MatrixControl)
            adapter._send_tasks = {}
            adapter._delivery_lock = asyncio.Lock()
            adapter._close_task = None
            adapter._closed = False
            control.Control.__init__(adapter)
            adapter._room_id, adapter._admin_room_id = "normal", "admin"
            adapter._init_done = asyncio.Event()
            adapter._init_done.set()
            adapter._client = mock.Mock()
            adapter._client.room_send = mock.AsyncMock(side_effect=nio.exceptions.OlmUnverifiedDeviceError("SECRET_SENTINEL"))
            sender = adapter._client.room_send
            async def matrix_ingress(text: str) -> None:
                assert isinstance(adapter, MatrixControl)
                event = mock.Mock(spec=nio.RoomMessageText, body=text)
                await adapter._message_callback(mock.Mock(room_id="normal"), event)
            ingress = matrix_ingress
        processed = asyncio.Event()
        async def run() -> None:
            for text in ("!ping", "!help", "!authorize not-a-url"):
                await ingress(text)
            processed.set()
            await asyncio.Event().wait()
        setup_patch = mock.patch.object(adapter, "setup")
        setup_patch.start()
        self.addCleanup(setup_patch.stop)
        run_patch = mock.patch.object(adapter, "run", new=run)
        run_patch.start()
        self.addCleanup(run_patch.stop)
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        multi = control.MultiControl([adapter, self.chat, mqtt])
        multi.callback = self.app
        self.app.control = multi
        broker = BrokerClient()
        with mock.patch("aiomqtt.Client", return_value=broker), self.assertLogs(level="INFO") as logs:
            task = self.start(multi.run())
            await asyncio.wait_for(processed.wait(), 1)
            self.assertEqual(sender.call_count, 3)
            self.assertFalse(task.done())
            self.assertFalse(self.chat.stopped)
            self.assertFalse(broker.closed)
            self.assertEqual(self.chat.messages, [])
            if kind == "slack":
                recovered = True
            else:
                sender.side_effect = None
                sender.return_value = mock.Mock(event_id="delivered")
            await ingress("!ping")
            self.assertEqual(sender.call_count, 4)
            await self.chat.process_message(control.CommandContext(False, self.chat), "!ping")
            self.assertEqual(self.chat.messages[-1][1], "pong")
            self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
            self.assertIn("Traceback (most recent call last)", "\n".join(logs.output))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await mqtt.close()

    async def test_slack_send_failures_do_not_escape_actual_ingress(self) -> None:
        await self._reply_failure_case("slack")

    async def test_matrix_verification_failures_do_not_escape_actual_ingress(self) -> None:
        await self._reply_failure_case("matrix")

    async def test_programming_failure_and_cancellation_still_escape_ingress(self) -> None:
        invoke_patch = mock.patch.object(self.app._commands, "invoke", side_effect=RuntimeError("SECRET_SENTINEL"))
        invoke = invoke_patch.start()
        self.addCleanup(invoke_patch.stop)
        with self.assertRaises(RuntimeError):
            await self.chat.process_message(control.CommandContext(False, self.chat), "!help")
        invoke.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.chat.process_message(control.CommandContext(False, self.chat), "!help")

    async def test_slack_interrupted_discovery_empty_and_absent_state_recover(self) -> None:
        session = mock.Mock(close=mock.AsyncMock())
        blocked: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("teslabot.slack.aiohttp.ClientSession", return_value=session), \
             mock.patch("teslabot.slack.WebClient") as web:
            web.return_value.api_call.return_value = blocked
            first = SlackControl(self.env)
            setup = self.start(first.setup())
            await asyncio.sleep(0)
            await self.state.save()
            self.assertEqual(self.state["slack"]["channel_id"], "")
            setup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await setup
            await first.close()
            for stored in ({"channel_id": ""}, {}):
                self.state["slack"] = stored
                restarted = SlackControl(self.env)
                def response(**kwargs: Any) -> asyncio.Future[dict[str, Any]]:
                    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                    future.set_result({"ok": True, "channels": [{"id": "normal", "name": "normal"}]})
                    return future
                web.return_value.api_call = mock.Mock(side_effect=response)
                await restarted.setup()
                self.assertEqual(web.return_value.api_call.call_args.kwargs["api_method"], "users.conversations")
                self.assertEqual(restarted._channel_id, "normal")
                with mock.patch.object(restarted, "process_message") as process_message:
                    for channel in ("normal", "admin"):
                        await restarted._process_event({"channel": channel, "text": "!ping"})
                    self.assertEqual(process_message.await_count, 2)
                for admin, channel in ((False, "normal"), (True, "admin")):
                    await restarted.send_message(control.MessageContext(admin), "reply")
                    self.assertEqual(web.return_value.api_call.call_args.kwargs["json"]["channel"], channel)
                await restarted.close()

    async def _timer_case(self, problem: str) -> None:
        executed = asyncio.Event()
        completed = asyncio.Event()
        self.app.authorized = self.app.tesla.authorized = problem != "unauthenticated"
        if problem == "filtered":
            self.app.override_vehicles_lc = {"kept"}
            vehicles = mock.patch.object(self.app.tesla, "vehicle_list", return_value=[{"display_name": "RemovedCar"}, {"display_name": "Kept"}])
            vehicles.start()
            self.addCleanup(vehicles.stop)
        original_execute = self.app._execute_vehicle
        attempts = 0
        async def execute(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
            nonlocal attempts
            attempts += 1
            if problem == "unauthenticated" and attempts == 1:
                try:
                    return await original_execute(*args, **kwargs)
                finally:
                    self.app.tesla.authorized = True
                    self.app._auth_changed(True)
            if problem == "transient" and not executed.is_set():
                executed.set()
                raise requests.Timeout("SECRET_SENTINEL")
            executed.set()
            completed.set()
            return {"display_name": "Car", "vin": "VIN"}, True
        execute_patch = mock.patch.object(self.app, "_execute_vehicle", side_effect=execute)
        execute_patch.start()
        self.addCleanup(execute_patch.stop)
        first_command = ["ac", "on", "RemovedCar"] if problem in ("missing", "filtered") else ["ac", "on"]
        now = datetime.datetime.now()
        recurring = scheduler.Periodic(mock.AsyncMock(), now + datetime.timedelta(seconds=0.02),
                                       datetime.timedelta(days=1), appscheduler.SchedulerContext(appscheduler.AppTimerInfo(1, first_command, None)))
        valid = scheduler.OneShot(mock.AsyncMock(), now + datetime.timedelta(seconds=0.06),
                                  appscheduler.SchedulerContext(appscheduler.AppTimerInfo(2, ["ac", "on"], None)))
        self.state["timers"] = {str(entry.context.info.id): json.dumps(appscheduler.timer_entry_to_json(entry))
                                for entry in (recurring, valid)}
        await self.app.initialize()
        runtime = self.start(self.app.run())
        await asyncio.wait_for(completed.wait(), 1)
        await asyncio.sleep(0)
        self.assertFalse(runtime.done())
        self.assertTrue(self.state["timers"].has_key("1"))
        self.assertFalse(self.state["timers"].has_key("2"))
        self.assertTrue(executed.is_set())
        if problem == "unauthenticated":
            self.assertTrue(any("Error:" in message for _, message in self.chat.messages))
        saved = dict(self.state["timers"].items())
        runtime.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runtime
        await self.app.close()
        restored_state = FileState("tmp/review-restored-state.ini")
        restored_state["timers"] = saved
        storage = mock.patch.object(restored_state, "save_to_storage", new=mock.AsyncMock())
        storage.start()
        self.addCleanup(storage.stop)
        with mock.patch("teslabot.tesla.TeslaSession", FakeTesla):
            restored = tesla.App(self.multi, Env(self.cfg, restored_state))
        await restored.initialize()
        entries = await restored._scheduler._scheduler.get_entries()
        self.assertEqual([entry.context.info.id for entry in entries], [1])
        assert isinstance(entries[0], scheduler.Periodic)
        self.assertGreater(entries[0].next_time, now)
        await restored.close()

    async def test_persisted_missing_vehicle_timer_retained_and_next_timer_runs(self) -> None:
        await self._timer_case("missing")

    async def test_filtered_vehicle_timer_isolated_and_retained_after_restart(self) -> None:
        await self._timer_case("filtered")

    async def test_unauthenticated_timer_does_not_stop_scheduler(self) -> None:
        await self._timer_case("unauthenticated")

    async def test_transient_timer_failure_does_not_stop_later_timer(self) -> None:
        await self._timer_case("transient")

    async def test_timer_programming_failure_is_still_terminal(self) -> None:
        self.app.authorized = True
        invoke = mock.patch.object(self.app._commands, "invoke", side_effect=RuntimeError("SECRET_SENTINEL"))
        invoke.start()
        self.addCleanup(invoke.stop)
        info = appscheduler.AppTimerInfo(1, ["ac", "on"], None)
        entry = scheduler.OneShot(mock.AsyncMock(), datetime.datetime.now() + datetime.timedelta(seconds=0.02), appscheduler.SchedulerContext(info))
        entry._callback = lambda: self.app._scheduler._activate_timer(entry)
        await self.app._scheduler._scheduler.add(entry)
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.app.run(), 1)

    async def test_successful_token_exchange_supersedes_startup_notice(self) -> None:
        entered, release = threading.Event(), threading.Event()
        original = self.app.tesla.fetch_token
        def fetch(**kwargs: Any) -> None:
            entered.set()
            release.wait(1)
            original(**kwargs)
        fetch_patch = mock.patch.object(self.app.tesla, "fetch_token", new=fetch)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        authorization = mock.patch.object(self.app.tesla, "authorization_url", return_value="https://example.com/authorize")
        authorization_url = authorization.start()
        self.addCleanup(authorization.stop)
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        multi = control.MultiControl([self.chat, mqtt])
        multi.callback = self.app
        self.app.control = multi
        online = asyncio.Event()
        broker = BrokerClient(online)
        with mock.patch("aiomqtt.Client", return_value=broker):
            adapters = self.start(multi.run())
            authorize = self.start(self.chat.process_message(control.CommandContext(True, self.chat), "!authorize https://example.com/callback"))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            runtime = self.start(self.app.run())
            await asyncio.sleep(0.01)
            release.set()
            await asyncio.wait_for(authorize, 1)
            await asyncio.wait_for(online.wait(), 1)
            self.assertTrue(self.app.authorized)
            self.assertFalse(runtime.done())
            self.assertFalse(adapters.done())
            self.assertTrue(mqtt._current())
            authorization_url.assert_not_called()
            self.assertFalse(any("Authorization URL:" in text for _, text in self.chat.messages))
            adapters.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await adapters
        await mqtt.close()

    async def test_logout_supersedes_startup_notice_waiting_for_auth_gate(self) -> None:
        authorization = mock.patch.object(self.app.tesla, "authorization_url", return_value="https://example.com/authorize")
        authorization_url = authorization.start()
        self.addCleanup(authorization.stop)
        await self.app._auth_lock.acquire()
        try:
            logout_task = self.start(self.app._command_logout(control.CommandContext(True, self.chat), ()))
            await asyncio.sleep(0)
            runtime = self.start(self.app.run())
            await asyncio.sleep(0.01)
        finally:
            self.app._auth_lock.release()
        await asyncio.wait_for(logout_task, 1)
        await asyncio.sleep(0.01)
        self.assertFalse(runtime.done())
        authorization_url.assert_not_called()
        self.assertFalse(any("Authorization URL:" in text for _, text in self.chat.messages))

    async def test_startup_auth_notice_timeout_is_optional_but_programming_fault_is_terminal(self) -> None:
        authorization = mock.patch.object(self.app.tesla, "authorization_url", side_effect=requests.Timeout("SECRET_SENTINEL"))
        authorization_url = authorization.start()
        self.addCleanup(authorization.stop)
        with self.assertLogs(level="WARNING") as logs:
            runtime = self.start(self.app.run())
            for _ in range(100):
                if authorization_url.called:
                    break
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
        self.assertFalse(runtime.done())
        self.assertFalse(any("Authorization URL:" in text for _, text in self.chat.messages))
        self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
        runtime.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runtime
        await self.app.close()
        authorization_url.side_effect = RuntimeError("SECRET_SENTINEL")
        with self.assertRaises(RuntimeError):
            await self.app.run()

    async def _discovery_retry_case(self, logout: bool = False) -> None:
        response = requests.Response()
        response.status_code = 503
        self.app.authorized = self.app.tesla.authorized = True
        vehicles = mock.patch.object(self.app.tesla, "vehicle_list", side_effect=[requests.HTTPError(response=response),
                                      requests.Timeout("SECRET_SENTINEL"), requests.ConnectionError("SECRET_SENTINEL"), []])
        vehicle_list = vehicles.start()
        self.addCleanup(vehicles.stop)
        async def one_attempt(fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
            return await fn()
        retry_patch = mock.patch.object(self.app, "_retry", new=one_attempt)
        retry_patch.start()
        self.addCleanup(retry_patch.stop)
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        multi = control.MultiControl([self.chat, mqtt])
        multi.callback = self.app
        self.app.control = multi
        ready = asyncio.Event()
        original_reconcile = mqtt._reconcile
        async def reconcile(client: Any, generation: int) -> bool:
            result = await original_reconcile(client, generation)
            if result:
                ready.set()
            return result
        reconcile_patch = mock.patch.object(mqtt, "_reconcile", new=reconcile)
        reconcile_patch.start()
        self.addCleanup(reconcile_patch.stop)
        clients: list[BrokerClient] = []
        delays: list[float] = []
        def connect(*args: Any, **kwargs: Any) -> BrokerClient:
            client = BrokerClient()
            clients.append(client)
            return client
        async def retry(delay: float) -> None:
            delays.append(delay)
            self.assertTrue(all(client.closed for client in clients))
            self.assertFalse(any(payload == "online" for client in clients for _, payload in client.publications))
            await self.chat.process_message(control.CommandContext(False, self.chat), "!ping")
            if logout and len(delays) == 3:
                await self.app._command_logout(control.CommandContext(True, self.chat), ())
            await asyncio.sleep(0)
        wait_retry = mock.patch.object(mqtt, "_wait_retry", new=retry)
        wait_retry.start()
        self.addCleanup(wait_retry.stop)
        start_scheduler = mock.patch.object(self.app._scheduler._scheduler, "start", wraps=self.app._scheduler._scheduler.start)
        with mock.patch("aiomqtt.Client", side_effect=connect), start_scheduler as scheduler_start:
            adapters = self.start(multi.run())
            runtime = self.start(self.app.run())
            await asyncio.wait_for(ready.wait(), 1)
            self.assertFalse(adapters.done())
            self.assertFalse(runtime.done())
            self.assertEqual(delays, [5, 10, 20])
            self.assertEqual(mqtt._generation, self.app.auth_generation)
            self.assertEqual(mqtt._current(), not logout)
            self.assertFalse(self.chat.stopped)
            scheduler_start.assert_awaited_once()
            calls = vehicle_list.call_count
            await asyncio.sleep(0.01)
            self.assertEqual(vehicle_list.call_count, calls)
            adapters.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await adapters
        await mqtt.close()

    async def test_duplicate_discovery_identity_is_terminal_not_retried(self) -> None:
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        self.app.authorized = self.app.tesla.authorized = True
        vehicles = mock.patch.object(self.app.tesla, "vehicle_list", return_value=[{"display_name": "One", "vin": "shared"},
                                      {"display_name": "Two", "vin": "shared"}])
        vehicles.start()
        self.addCleanup(vehicles.stop)
        with mock.patch.object(mqtt, "_wait_retry") as wait_retry, mock.patch("aiomqtt.Client", return_value=BrokerClient()):
            with self.assertRaises(control.ConfigError):
                await mqtt.run()
        wait_retry.assert_not_awaited()
        await mqtt.close()

    async def test_tesla_discovery_failures_retry_locally_until_recovery(self) -> None:
        await self._discovery_retry_case()

    async def test_logout_interrupts_failed_discovery_without_old_generation_online(self) -> None:
        await self._discovery_retry_case(logout=True)

    async def test_retry_backoff_is_auth_interruptible_but_invalid_manifest_is_terminal(self) -> None:
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        waiting = self.start(mqtt._wait_retry(60))
        await asyncio.sleep(0)
        self.app._auth_changed(False)
        await asyncio.wait_for(waiting, 1)
        self.state["mqtt_owned"] = {mqtt._manifest_key: '["not-an-owned-id"]'}
        with mock.patch("aiomqtt.Client", return_value=BrokerClient()):
            with self.assertRaises(control.ConfigError):
                await mqtt.run()
        await mqtt.close()

    async def test_app_retries_actual_requests_503_and_timeout(self) -> None:
        response = requests.Response()
        response.status_code = 503
        fn = mock.Mock(side_effect=[requests.HTTPError(response=response), requests.Timeout(), "recovered"])
        original_sleep = asyncio.sleep
        async def immediate(delay: float) -> None:
            await original_sleep(0)
        with mock.patch("teslabot.tesla.asyncio.sleep", side_effect=immediate) as sleep:
            self.assertEqual(await self.app._retry_to_async(fn), "recovered")
        self.assertEqual(fn.call_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_default_cli_logging_emits_readiness_retry_and_sdk_diagnostics(self) -> None:
        root = logging.getLogger()
        root_level = root.level
        handlers = list(root.handlers)
        names = ("teslabot", "teslabot.main", "teslabot.control", "teslabot.tesla", "teslabot.scheduler",
                 "teslabot.slack", "teslabot.mqtt", "teslapy", "requests_oauthlib", "oauthlib", "slack", "nio", "aiohttp", "urllib3")
        previous = {name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                           list(logging.getLogger(name).handlers)) for name in names}
        output = io.StringIO()
        root.setLevel(logging.WARNING)
        logging.getLogger("teslabot").setLevel(logging.NOTSET)
        logging.getLogger("teslabot.mqtt").setLevel(logging.NOTSET)
        logging.getLogger("teslabot.slack").setLevel(logging.NOTSET)
        cfg = Config("unused", {"common": {"storage": "local", "control": "slack,mqtt"},
                                "tesla": {"email": "SECRET_SENTINEL"},
                                "mqtt": {"host": "localhost", "username": "SECRET_SENTINEL", "password": "SECRET_SENTINEL"}})
        class AuthorizedTesla(FakeTesla):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.authorized = True
            def vehicle_list(self) -> list[dict[str, str]]:
                return [{"vin": "SECRET_SENTINEL", "display_name": "Car"}]
        original_run = self.chat.run
        async def chat_run() -> None:
            logging.getLogger("teslabot.slack").info("Slack ready")
            logging.getLogger("teslapy").error("SECRET_SENTINEL SDK payload")
            await original_run()
        online = asyncio.Event()
        broker = BrokerClient(online)
        original_sleep = asyncio.sleep
        async def immediate_retry(adapter: MqttControl, delay: float) -> None:
            await original_sleep(0)
        async def cli() -> None:
            try:
                await main.async_main()
            except SystemExit:
                raise AssertionError(output.getvalue()) from None
        try:
            with mock.patch.object(self.chat, "run", new=chat_run), contextlib.redirect_stdout(output), \
                 mock.patch.object(config, "get_args", return_value=mock.Mock(version=False)), \
                 mock.patch.object(config, "Config", return_value=cfg), \
                 mock.patch.object(main, "filestate", new=mock.Mock(FileState=mock.Mock(return_value=self.state))), \
                 mock.patch("teslabot.slack.SlackControl", return_value=self.chat), \
                 mock.patch("teslabot.tesla.TeslaSession", AuthorizedTesla), \
                 mock.patch("aiomqtt.Client", side_effect=[aiomqtt.MqttError("SECRET_SENTINEL"), broker]), \
                 mock.patch.object(MqttControl, "_wait_retry", new=immediate_retry):
                runtime = self.start(cli())
                await asyncio.sleep(0.05)
                if runtime.done():
                    await runtime
                await asyncio.wait_for(online.wait(), 1)
                self.assertFalse(runtime.done())
                runtime.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await runtime
            text = output.getvalue()
            for expected in ("Selected controls: slack,mqtt", "Slack ready", "MQTT initialized",
                             "MQTT online: generation 0, 1 vehicles", "MQTT reconciliation interrupted: MqttError"):
                self.assertIn(expected, text)
            self.assertIn("SECRET_SENTINEL SDK payload", text)
            self.assertIn("MqttError: SECRET_SENTINEL", text)
            self.assertIn("Traceback (most recent call last)", text)
        finally:
            root.setLevel(root_level)
            for handler in root.handlers[:]:
                if handler not in handlers:
                    root.removeHandler(handler)
                    handler.close()
            for name, (level, propagate, owned_handlers) in previous.items():
                logger = logging.getLogger(name)
                logger.setLevel(level)
                logger.propagate = propagate
                for handler in logger.handlers[:]:
                    if handler not in owned_handlers:
                        logger.removeHandler(handler)
                        handler.close()
