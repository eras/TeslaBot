import asyncio
import json
import unittest
import urllib.parse
from unittest import mock

import requests
import requests.adapters
import urllib3.exceptions

from teslabot import control, tesla
from teslabot.mqtt import MqttControl
import tests.test_multi_control_review as review


class ResponseAdapter(requests.adapters.BaseAdapter):
    def __init__(self, failures):
        self.failures = failures
        self.products = 0

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        product = urllib.parse.urlparse(request.url or "").path.endswith("/products")
        interrupted = product and self.failures != 0
        if product:
            self.products += 1
            if self.failures > 0:
                self.failures -= 1
        payload = {"response": [{"display_name": "Test car", "vin": "TESTVIN", "vehicle_id": 1,
                                  "id": 1, "id_s": "1"}] if product else []}
        class Raw:
            def stream(self, chunk_size, decode_content=True):
                if interrupted:
                    raise urllib3.exceptions.ProtocolError("SECRET_SENTINEL interrupted response")
                yield json.dumps(payload).encode()

            def close(self):
                pass

            def release_conn(self):
                pass

        response = requests.Response()
        response.status_code = 200
        response.url = request.url or ""
        response.request = request
        response.raw = Raw()
        return response

    def close(self):
        pass


class InterruptedResponseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        fixture = review.ReviewRegressionTests()
        fixture.setUp()
        self.app, self.chat, self.env = fixture.app, fixture.chat, fixture.env
        self.sdk = tesla.TeslaSession("test@example.com", timeout=30,
                                      cache_loader=lambda: {}, cache_dumper=lambda value: None)
        self.sdk.token = {"access_token": "test-token", "token_type": "Bearer"}
        self.adapter = ResponseAdapter(0)
        self.sdk.mount("https://", self.adapter)
        self.app.tesla = self.sdk
        self.app.authorized = True
        self.tasks = []
        self.sleep = asyncio.sleep
        async def quick_retry(delay):
            await self.sleep(0)
        self.retry_sleep = mock.patch("teslabot.tesla.asyncio.sleep", side_effect=quick_retry)
        self.retry_sleep.start()

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.retry_sleep.stop()
        await self.app.close()

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def test_real_requests_wrapper_is_transient_and_app_recovers(self):
        self.adapter.failures = 2
        with self.assertRaises(requests.exceptions.ChunkedEncodingError) as caught:
            self.sdk.vehicle_list()
        self.assertIsInstance(caught.exception.args[0], urllib3.exceptions.ProtocolError)
        self.assertTrue(tesla.is_transient_error(caught.exception))
        vehicles = await self.app._get_vehicle_list()
        self.assertEqual(vehicles[0]["display_name"], "Test car")
        self.assertEqual(self.adapter.products, 3)
        self.assertTrue(self.app.authorized)

    async def test_chat_real_interrupted_response_recovers_under_supervision(self):
        self.adapter.failures = 1
        healthy = review.Chat()
        multi = control.MultiControl([self.chat, healthy])
        multi.callback = self.app
        self.app.control = multi
        processed = asyncio.Event()
        original_run = self.chat.run
        async def ingress():
            await self.chat.process_message(control.CommandContext(False, self.chat), "!vehicles")
            processed.set()
            await original_run()
        self.chat.run = ingress
        with self.assertLogs("teslabot", "DEBUG") as logs:
            runtime = self.start(multi.run())
            await asyncio.wait_for(processed.wait(), 1)
            self.assertFalse(runtime.done())
            self.assertFalse(healthy.stopped)
            self.assertIn("Test car", self.chat.messages[-1][1])
            self.assertEqual(self.adapter.products, 2)
            self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
            self.assertIn("Traceback (most recent call last)", "\n".join(logs.output))

    async def test_exhausted_chat_and_mqtt_retries_keep_healthy_adapters_alive(self):
        self.adapter.failures = -1
        healthy = review.Chat()
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        multi = control.MultiControl([self.chat, healthy, mqtt])
        multi.callback = self.app
        self.app.control = multi
        processed, backing_off = asyncio.Event(), asyncio.Event()
        original_run = self.chat.run
        async def ingress():
            await self.chat.process_message(control.CommandContext(False, self.chat), "!vehicles")
            processed.set()
            await original_run()
        self.chat.run = ingress
        async def retry(delay):
            self.assertEqual(delay, 5)
            backing_off.set()
            await asyncio.Event().wait()
        mqtt._wait_retry = retry
        broker = review.BrokerClient()
        with mock.patch("aiomqtt.Client", return_value=broker), self.assertLogs("teslabot", "DEBUG") as logs:
            runtime = self.start(multi.run())
            await asyncio.wait_for(processed.wait(), 1)
            await asyncio.wait_for(backing_off.wait(), 1)
            self.assertEqual(self.adapter.products, 30)
            self.assertFalse(runtime.done())
            self.assertFalse(healthy.stopped)
            self.assertEqual([text for _, text in self.chat.messages], ["Tesla request failed; please retry"])
            self.assertFalse(any(payload == "online" for _, payload in broker.publications))
            self.assertTrue(self.app.authorized)
            await healthy.process_message(control.CommandContext(False, healthy), "!ping")
            self.assertEqual(healthy.messages[-1][1], "pong")
            self.adapter.failures = 0
            await self.chat.process_message(control.CommandContext(False, self.chat), "!vehicles")
            self.assertIn("Test car", self.chat.messages[-1][1])
            self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runtime
        await mqtt.close()

    async def test_mqtt_retries_exhausted_real_response_then_recovers_without_polling(self):
        self.adapter.failures = 15
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        multi = control.MultiControl([self.chat, mqtt])
        multi.callback = self.app
        self.app.control = multi
        backing_off, release, online = asyncio.Event(), asyncio.Event(), asyncio.Event()
        clients = []
        def connect(*args, **kwargs):
            client = review.BrokerClient(online)
            clients.append(client)
            return client
        async def retry(delay):
            self.assertEqual(delay, 5)
            backing_off.set()
            await release.wait()
        mqtt._wait_retry = retry
        with mock.patch("aiomqtt.Client", side_effect=connect), self.assertLogs("teslabot", "DEBUG") as logs:
            runtime = self.start(multi.run())
            await asyncio.wait_for(backing_off.wait(), 1)
            self.assertEqual(self.adapter.products, 15)
            self.assertTrue(clients[0].closed)
            self.assertFalse(any(payload == "online" for _, payload in clients[0].publications))
            await self.chat.process_message(control.CommandContext(False, self.chat), "!ping")
            release.set()
            await asyncio.wait_for(online.wait(), 1)
            self.assertFalse(runtime.done())
            self.assertFalse(self.chat.stopped)
            self.assertTrue(mqtt._current())
            self.assertEqual(len(mqtt.vehicles), 1)
            self.assertEqual(self.adapter.products, 16)
            await self.sleep(0.01)
            self.assertEqual(self.adapter.products, 16)
            self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runtime
        await mqtt.close()

    async def test_logout_interrupts_real_response_recovery_and_keeps_mqtt_offline(self):
        self.adapter.failures = -1
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        multi = control.MultiControl([self.chat, mqtt])
        multi.callback = self.app
        self.app.control = multi
        backing_off, reconciled = asyncio.Event(), asyncio.Event()
        original_wait, original_reconcile = mqtt._wait_retry, mqtt._reconcile
        async def retry(delay):
            backing_off.set()
            await original_wait(60)
        async def reconcile(client, generation):
            result = await original_reconcile(client, generation)
            if result and not self.app.authorized:
                reconciled.set()
            return result
        mqtt._wait_retry, mqtt._reconcile = retry, reconcile
        clients = []
        def connect(*args, **kwargs):
            client = review.BrokerClient()
            clients.append(client)
            return client
        with mock.patch("aiomqtt.Client", side_effect=connect), self.assertLogs("teslabot", "DEBUG") as logs:
            runtime = self.start(multi.run())
            await asyncio.wait_for(backing_off.wait(), 1)
            await self.app._command_logout(control.CommandContext(True, self.chat), ())
            await asyncio.wait_for(reconciled.wait(), 1)
            self.assertFalse(runtime.done())
            self.assertFalse(self.chat.stopped)
            self.assertFalse(self.app.authorized)
            self.assertFalse(self.sdk.authorized)
            self.assertEqual(mqtt._generation, self.app.auth_generation)
            self.assertEqual(mqtt.vehicles, {})
            self.assertEqual(self.adapter.products, 15)
            self.assertFalse(any(payload == "online" for client in clients for _, payload in client.publications))
            self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runtime
        await mqtt.close()

    async def test_unrelated_programming_and_configuration_errors_remain_terminal(self):
        for error in (RuntimeError(), ValueError(), requests.exceptions.InvalidURL(),
                      requests.exceptions.InvalidSchema(), control.ConfigError()):
            self.assertFalse(tesla.is_transient_error(error))
        self.sdk.vehicle_list = mock.Mock(side_effect=RuntimeError("SECRET_SENTINEL"))
        mqtt = MqttControl(self.env)
        mqtt.set_app(self.app)
        mqtt._wait_retry = mock.AsyncMock()
        multi = control.MultiControl([self.chat, mqtt])
        multi.callback = self.app
        self.app.control = multi
        with mock.patch("aiomqtt.Client", return_value=review.BrokerClient()), self.assertLogs("teslabot", "DEBUG") as logs:
            with self.assertRaises(RuntimeError):
                await multi.run()
        self.assertTrue(self.chat.stopped)
        mqtt._wait_retry.assert_not_awaited()
        self.assertIn("SECRET_SENTINEL", "\n".join(logs.output))
        with self.assertRaises(RuntimeError):
            await self.chat.process_message(control.CommandContext(False, self.chat), "!vehicles")
        await mqtt.close()
