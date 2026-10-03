import asyncio
import contextlib
import datetime
import json
import threading
import types
import unittest
from unittest import mock

import aiomqtt

from teslabot import control, tesla
from teslabot.config import Config
from teslabot.env import Env
from teslabot.filestate import FileState
from teslabot.mqtt import MqttControl, _Session
import tests.test_sdk_boundary as sdk_tests
import tests.test_multi_control as chat_tests


class Clock:
    def __init__(self):
        self.waits = []
        self.started = asyncio.Queue()
        self.real_sleep = asyncio.sleep

    async def sleep(self, seconds):
        assert 0 <= seconds <= 300
        event = asyncio.Event()
        self.waits.append((seconds, event))
        self.started.put_nowait(event)
        await event.wait()

    @contextlib.contextmanager
    def install(self):
        async def wait(adapter, deadline):
            await self.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        with mock.patch.object(MqttControl, "_wait_refresh", new=wait):
            yield

    async def next(self):
        return await asyncio.wait_for(self.started.get(), 1)


class Broker:
    def __init__(self):
        self.messages = self.receive()
        self.incoming = asyncio.Queue()
        self.publications = []
        self.subscriptions = []
        self.retained = {}
        self.online = asyncio.Event()
        self.closed = False
        self.publish_error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def receive(self):
        while True:
            yield await self.incoming.get()

    async def publish(self, topic, payload, **kwargs):
        if self.publish_error is not None and topic.endswith("/state") and "/action_refresh_delay/" not in topic:
            raise self.publish_error
        self.publications.append((topic, payload, kwargs))
        if kwargs.get("retain"):
            self.retained[topic] = payload
        if payload == "online":
            self.online.set()

    async def subscribe(self, topic, **kwargs):
        self.subscriptions.append(topic)

    async def command(self, topic, payload="ON", retained=False):
        await self.incoming.put(types.SimpleNamespace(topic=topic, payload=payload.encode(), retain=retained))


def snapshot(identity="0123456789abcdef", battery=80):
    return tesla.VehicleSnapshot(identity, "Synthetic car", datetime.datetime.now(datetime.timezone.utc),
                                 battery, "Disconnected", 90, 16, False, 0, 20, 10, "C", {"diagnostic": "kept"})


class DelayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        self.state = FileState("tmp/delay-test-state.ini")
        self.state.save_to_storage = mock.AsyncMock()
        self.mqtt = self.build()
        self.app = mock.Mock(auth_events=[], authorized=True, auth_generation=0)
        self.app._get_vehicle_list = mock.AsyncMock(return_value=[])
        self.app.refresh_vehicle = mock.AsyncMock(side_effect=lambda *args, **kwargs: snapshot(kwargs["vehicle_id"]))
        async def action(name, *args, **kwargs):
            return tesla.ActionResult(kwargs["vehicle_id"], name, args[1], True)
        async def ac(*args, **kwargs): return await action("ac", *args, **kwargs)
        async def sauna(*args, **kwargs): return await action("sauna", *args, **kwargs)
        async def charge(*args, **kwargs): return await action("charge_limit", *args, **kwargs)
        self.app.set_ac = mock.AsyncMock(side_effect=ac)
        self.app.set_sauna = mock.AsyncMock(side_effect=sauna)
        self.app.set_charge_limit = mock.AsyncMock(side_effect=charge)
        self.mqtt.set_app(self.app)
        self.id1, self.id2 = "0123456789abcdef", "abcdef0123456789"
        self.mqtt.vehicles = {self.id1: "One", self.id2: "Two"}
        self.mqtt._generation = 0
        self.broker = Broker()
        self.session = _Session(self.broker, 0)
        self.mqtt._session = self.session
        self.clock = Clock()
        self.tasks = []
        self.releases = []

    def build(self, settings=None):
        values = {"host": "localhost"}
        values.update(settings or {})
        return MqttControl(Env(Config("unused", {"mqtt": values}), self.state))

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.mqtt.close()

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def handle(self, operation="ac", payload="ON", identity=None, retained=False):
        await self.mqtt._handle(self.broker, f"teslabot/{identity or self.id1}/{operation}/set", payload, retained)

    async def setting(self, value, retained=False):
        await self.mqtt._handle(self.broker, "teslabot/action_refresh_delay/set", value, retained)

    async def release_job(self, event, identity=None):
        task = self.session.jobs[identity or self.id1]
        event.set()
        await asyncio.wait_for(task, 1)

    def state_publications(self):
        return [call for call in self.broker.publications if call[0].endswith("/state") and "/action_refresh_delay/" not in call[0]]

    async def test_default_and_every_action_delay_result_then_one_observed_read(self):
        self.assertEqual(self.mqtt.action_refresh_delay, 5)
        with self.clock.install():
            for operation, payload in (("ac", "ON"), ("ac", "OFF"), ("sauna", "ON"), ("sauna", "OFF"), ("charge_limit", "70")):
                self.broker.publications.clear()
                before = self.app.refresh_vehicle.await_count
                await self.handle(operation, payload)
                event = await self.clock.next()
                self.assertTrue(self.broker.publications[0][0].endswith("/result"))
                self.assertTrue(json.loads(self.broker.publications[0][1])["success"])
                self.assertEqual(self.app.refresh_vehicle.await_count, before)
                self.assertEqual(self.state_publications(), [])
                self.assertGreater(self.clock.waits[-1][0], 4.9)
                self.assertLessEqual(self.clock.waits[-1][0], 5)
                await self.release_job(event)
                self.assertEqual(self.app.refresh_vehicle.await_count, before + 1)
                state = json.loads(self.state_publications()[0][1])
                self.assertFalse(state["climate_on"])
                self.assertEqual(state["charge_limit"], 90)
        self.assertEqual(self.session.jobs, {})

    async def test_coalescing_failed_supersession_invalid_paths_and_independent_vehicles(self):
        with self.clock.install():
            await self.handle()
            old = await self.clock.next()
            old_task = self.session.jobs[self.id1]
            for operation, payload, identity, retained in (("ac", "bad", self.id1, False),
                                                          ("charge_limit", "101", self.id1, False),
                                                          ("ac", "OFF", self.id1, True),
                                                          ("authorize", "ON", self.id1, False),
                                                          ("ac", "ON", "unknown", False)):
                await self.handle(operation, payload, identity, retained)
                self.assertIs(self.session.jobs[self.id1], old_task)
            await self.handle(identity=self.id2)
            other = await self.clock.next()
            for _ in range(3):
                await self.handle("ac", "OFF")
                latest = await self.clock.next()
            self.assertTrue(old_task.cancelled())
            old.set()
            self.assertEqual(self.app.refresh_vehicle.await_count, 0)
            await self.release_job(other, self.id2)
            await self.release_job(latest)
            self.assertEqual(self.app.refresh_vehicle.await_count, 2)
            await self.handle()
            await self.clock.next()
            async def failed(*args, **kwargs): return tesla.ActionResult(self.id1, "ac", False, False, "rejected detail")
            self.app.set_ac.side_effect = failed
            await self.handle("ac", "OFF")
            self.assertNotIn(self.id1, self.session.jobs)
            self.assertEqual(self.app.refresh_vehicle.await_count, 2)

    async def test_manual_refresh_and_zero_mode_are_immediate_and_ordered(self):
        with self.clock.install():
            await self.handle()
            await self.clock.next()
            await self.handle("refresh", "manual payload")
            self.assertEqual(self.app.refresh_vehicle.await_count, 1)
            self.assertEqual(self.session.jobs, {})
            self.mqtt.action_refresh_delay = 0
            self.broker.publications.clear()
            await self.handle()
            self.assertEqual([call[0].split("/")[-1] for call in self.broker.publications], ["result", "state"])
            self.assertEqual(self.session.jobs, {})
        self.assertEqual(len(self.clock.waits), 1)

    async def test_setting_commit_restart_namespace_and_no_job_retiming_or_api(self):
        with self.clock.install():
            await self.handle()
            event = await self.clock.next()
            task = self.session.jobs[self.id1]
            before = self.app.set_ac.await_count
            await self.setting("12")
            self.assertEqual(self.mqtt.action_refresh_delay, 12)
            self.assertIs(self.session.jobs[self.id1], task)
            self.assertEqual(self.clock.waits[-1][0] <= 5, True)
            self.assertEqual(self.app.set_ac.await_count, before)
            self.assertEqual(self.app.refresh_vehicle.await_count, 0)
            self.assertEqual(self.app.auth_generation, 0)
            restarted = self.build({"action_refresh_delay": "3"})
            self.assertEqual(restarted.action_refresh_delay, 12)
            self.assertEqual(self.build({"prefix": "different"}).action_refresh_delay, 5)
            self.assertEqual(self.build({"host": "other-broker"}).action_refresh_delay, 5)
            await self.release_job(event)
            await self.handle()
            event = await self.clock.next()
            self.assertGreater(self.clock.waits[-1][0], 11.9)
            await self.release_job(event)

    async def test_setting_validation_retained_exact_routing_and_storage_failure(self):
        for value in ("-1", "301", "1.0", "true", "nan", "inf", "", " 5", "5 ", "05", "５", "+5"):
            with self.subTest(value=value):
                if value.strip() == value and value:
                    with self.assertRaises(control.ConfigError): self.build({"action_refresh_delay": value})
                before = self.mqtt.action_refresh_delay
                await self.setting(value)
                self.assertEqual(self.mqtt.action_refresh_delay, before)
        self.state.save_to_storage.assert_not_awaited()
        self.assertEqual(self.build({"action_refresh_delay": " 5 "}).action_refresh_delay, 5)
        await self.setting("10", retained=True)
        for topic in ("other/action_refresh_delay/set", "teslabot/unknown/set", "teslabot/action_refresh_delay/set/extra"):
            await self.mqtt._handle(self.broker, topic, "10", False)
        self.assertEqual(self.mqtt.action_refresh_delay, 5)
        self.state.save_to_storage.side_effect = OSError("storage diagnostic detail")
        with self.assertLogs("teslabot.mqtt", "ERROR") as logs:
            await self.setting("9")
        self.assertEqual(self.mqtt.action_refresh_delay, 5)
        self.assertIsNone(self.state.get("mqtt_action_refresh_delay", self.mqtt._manifest_key, fallback=None))
        self.assertEqual(self.broker.publications, [])
        self.assertIn("storage diagnostic detail", "\n".join(logs.output))
        self.state.save_to_storage.side_effect = None
        for value in ("0", "300"):
            await self.setting(value)
            self.assertEqual(self.mqtt.action_refresh_delay, int(value))
            self.assertEqual(self.broker.publications[-1][1], value)
        self.assertEqual(self.app.refresh_vehicle.await_count, 0)
        self.state["mqtt_action_refresh_delay"][self.mqtt._manifest_key] = "broken"
        with self.assertRaisesRegex(control.ConfigError, "configuration/state"):
            self.build()

    async def test_setting_publish_failure_after_commit_remains_durable(self):
        original = self.broker.publish
        async def publish(topic, payload, **kwargs):
            if "/action_refresh_delay/state" in topic:
                raise aiomqtt.MqttError("notification diagnostic detail")
            await original(topic, payload, **kwargs)
        self.broker.publish = publish
        with self.assertRaises(aiomqtt.MqttError): await self.setting("17")
        self.assertEqual(self.mqtt.action_refresh_delay, 17)
        self.assertEqual(self.build().action_refresh_delay, 17)
        self.assertEqual(self.state["mqtt_action_refresh_delay"][self.mqtt._manifest_key], "17")
        self.assertEqual(self.app.refresh_vehicle.await_count, 0)
        self.broker.publish = original
        await self.mqtt._reconcile(self.broker, 0)
        self.assertEqual(self.broker.retained["teslabot/action_refresh_delay/state"], "17")

    async def test_instance_discovery_and_subscription_before_online_with_zero_vehicles(self):
        self.app._get_vehicle_list.return_value = []
        await self.mqtt._reconcile(self.broker, 0)
        config_topic = "homeassistant/number/teslabot/action_refresh_delay/config"
        config = json.loads(self.broker.retained[config_topic])
        self.assertEqual(config["name"], "Action refresh delay")
        self.assertEqual(config["device"]["name"], "TeslaBot")
        self.assertEqual((config["min"], config["max"], config["step"]), (0, 300, 1))
        self.assertEqual(config["entity_category"], "config")
        self.assertFalse(config["retain"])
        self.assertEqual(config["qos"], 0)
        self.assertIn("teslabot/action_refresh_delay/set", self.broker.subscriptions)
        self.assertEqual(self.broker.retained["teslabot/action_refresh_delay/state"], "5")
        self.assertEqual(self.broker.publications[-1][1], "online")
        other = self.build({"prefix": "other/instance"})
        self.assertNotEqual(other._delay_discovery()["unique_id"], config["unique_id"])
        self.app.authorized = False
        self.broker.subscriptions.clear()
        await self.mqtt._reconcile(self.broker, 0)
        self.assertEqual(self.broker.publications[-1][1], "5")
        self.assertEqual(self.broker.subscriptions, [])

    async def test_failure_drops_observation_not_result_and_programming_failure_signals_owner(self):
        self.app.refresh_vehicle.side_effect = tesla.VehicleException("read diagnostic detail")
        with self.clock.install(), self.assertLogs("teslabot.mqtt", "WARNING") as logs:
            await self.handle()
            event = await self.clock.next()
            await self.release_job(event)
        self.assertEqual(self.state_publications(), [])
        self.assertTrue(json.loads(self.broker.publications[0][1])["success"])
        self.assertFalse(self.session.failed.is_set())
        self.assertIn("read diagnostic detail", "\n".join(logs.output))
        self.app.refresh_vehicle.side_effect = RuntimeError("background programming detail")
        with self.clock.install(), self.assertLogs("teslabot.mqtt", "ERROR"):
            await self.handle()
            event = await self.clock.next()
            await self.release_job(event)
        self.assertTrue(self.session.failed.is_set())
        self.assertIsInstance(self.session.error, RuntimeError)
        self.assertEqual(self.session.jobs, {})

    async def test_manual_refresh_drains_old_publication_before_new_read_and_publish(self):
        publishing, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        reads = 0
        async def read(*args, **kwargs):
            nonlocal reads
            reads += 1
            return snapshot(self.id1, battery=80 + reads)
        self.app.refresh_vehicle.side_effect = read
        original = self.broker.publish
        async def publish(topic, payload, **kwargs):
            if topic.endswith(f"/{self.id1}/state") and json.loads(payload)["battery_level"] == 81:
                publishing.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.set()
                    await release.wait()
                    # Model a packet already accepted by the old broker send.
                    await original(topic, payload, **kwargs)
            else:
                await original(topic, payload, **kwargs)
        self.broker.publish = publish
        with self.clock.install():
            await self.handle()
            event = await self.clock.next()
            event.set()
            await asyncio.wait_for(publishing.wait(), 1)
            old_token = self.session.tokens[self.id1]
            manual = self.start(self.handle("refresh", ""))
            await asyncio.wait_for(cleanup.wait(), 1)
            self.assertIsNot(self.session.tokens[self.id1], old_token)
            self.assertEqual(reads, 1)
            self.assertFalse(manual.done())
            release.set()
            await asyncio.wait_for(manual, 1)
        states = [json.loads(call[1])["battery_level"] for call in self.state_publications()]
        self.assertEqual(states, [81, 82])
        self.assertEqual(json.loads(self.broker.retained[f"teslabot/{self.id1}/state"])["battery_level"], 82)

    async def test_close_and_new_request_compete_without_second_cancel_of_read_cleanup(self):
        reading, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        cancellations = 0
        async def read(*args, **kwargs):
            nonlocal cancellations
            reading.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations += 1
                raise
            finally:
                cleanup.set()
                await release.wait()
        self.app.refresh_vehicle.side_effect = read
        with self.clock.install():
            await self.handle()
            (await self.clock.next()).set()
            await asyncio.wait_for(reading.wait(), 1)
            newer = self.start(self.handle("ac", "OFF"))
            self.session.receiver = newer
            await asyncio.wait_for(cleanup.wait(), 1)
            closing = self.start(self.mqtt.close())
            await self.clock.real_sleep(0)
            newer.cancel()
            closing.cancel()
            await self.clock.real_sleep(0)
            self.assertFalse(newer.done())
            self.assertFalse(closing.done())
            self.assertEqual(cancellations, 1)
            self.assertEqual(self.app.set_ac.await_count, 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError): await newer
            with self.assertRaises(asyncio.CancelledError): await closing
        self.assertEqual(cancellations, 1)
        self.assertEqual(self.session.jobs, {})
        self.assertEqual(self.state_publications(), [])

    async def test_result_publication_time_does_not_extend_completion_deadline(self):
        delay = 5
        original = self.broker.publish
        completion = asyncio.get_running_loop().time()
        async def publish(topic, payload, **kwargs):
            if topic.endswith("/result"):
                await self.clock.real_sleep(0.02)
            await original(topic, payload, **kwargs)
        self.broker.publish = publish
        with self.clock.install():
            await self.handle()
            event = await self.clock.next()
            requested, _ = self.clock.waits[-1]
            self.assertLess(requested, delay - 0.01)
            self.assertLessEqual(completion + 5, asyncio.get_running_loop().time() + requested + 0.01)
            await self.release_job(event)

    async def test_session_runtime_background_failure_and_broker_reconnect(self):
        for broker_failure in (False, True):
            with self.subTest(broker_failure=broker_failure):
                self.mqtt._closed = False
                self.app._get_vehicle_list.return_value = [{"display_name": "One"}]
                self.app._vehicle_id.return_value = self.id1
                first, second = Broker(), Broker()
                if broker_failure:
                    first.publish_error = aiomqtt.MqttError("broker publication detail")
                    self.app.refresh_vehicle.side_effect = lambda *args, **kwargs: snapshot(self.id1)
                else:
                    self.app.refresh_vehicle.side_effect = RuntimeError("supervised programming detail")
                retried = asyncio.Event()
                async def retry(delay): retried.set()
                self.mqtt._wait_retry = retry
                with self.clock.install(), mock.patch("aiomqtt.Client", side_effect=[first, second]), \
                     self.assertLogs("teslabot.mqtt", "ERROR"):
                    runtime = self.start(self.mqtt.run())
                    await asyncio.wait_for(first.online.wait(), 1)
                    old = self.mqtt._session
                    await first.command(f"teslabot/{self.id1}/ac/set")
                    (await self.clock.next()).set()
                    if broker_failure:
                        await asyncio.wait_for(second.online.wait(), 1)
                        self.assertTrue(retried.is_set())
                        self.assertTrue(first.closed)
                        self.assertIsNot(self.mqtt._session, old)
                        self.assertEqual(self.mqtt._session.generation, old.generation)
                        self.assertEqual(self.mqtt._session.jobs, {})
                        await self.clock.real_sleep(0.01)
                        self.assertFalse(any(call[0].endswith(f"/{self.id1}/state") for call in second.publications))
                        runtime.cancel()
                        with self.assertRaises(asyncio.CancelledError): await runtime
                    else:
                        with self.assertRaisesRegex(RuntimeError, "supervised programming detail"):
                            await asyncio.wait_for(runtime, 1)
                        self.assertTrue(first.closed)
                    self.assertEqual(old.jobs, {})
                    self.assertFalse(old.active)

    async def test_auth_change_stops_jobs_before_client_exit_no_replay(self):
        self.app._get_vehicle_list.return_value = [{"display_name": "One"}]
        self.app._vehicle_id.return_value = self.id1
        first, second = Broker(), Broker()
        with self.clock.install(), mock.patch("aiomqtt.Client", side_effect=[first, second]):
            runtime = self.start(self.mqtt.run())
            await asyncio.wait_for(first.online.wait(), 1)
            await first.command(f"teslabot/{self.id1}/ac/set")
            event = await self.clock.next()
            old = self.mqtt._session
            task = old.jobs[self.id1]
            self.app.auth_generation += 1
            self.mqtt._auth_event.set()
            await asyncio.wait_for(second.online.wait(), 1)
            self.assertTrue(task.cancelled())
            self.assertTrue(first.closed)
            self.assertFalse(old.active)
            event.set()
            await self.clock.real_sleep(0.01)
            self.assertEqual(self.app.refresh_vehicle.await_count, 0)
            self.assertEqual(old.jobs, {})
            self.assertEqual(self.mqtt._session.jobs, {})
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError): await runtime

    async def test_healthy_chat_receiver_and_setting_remain_responsive_during_delay(self):
        self.app._get_vehicle_list.return_value = [{"display_name": "One"}]
        self.app._vehicle_id.return_value = self.id1
        chat = chat_tests.Chat()
        broker = Broker()
        multi = control.MultiControl([chat, self.mqtt])
        with self.clock.install(), mock.patch("aiomqtt.Client", return_value=broker):
            runtime = self.start(multi.run())
            await asyncio.wait_for(broker.online.wait(), 1)
            await broker.command(f"teslabot/{self.id1}/ac/set")
            event = await self.clock.next()
            job = self.mqtt._session.jobs[self.id1]
            await chat.process_message(control.CommandContext(False, chat), "!ping")
            self.assertEqual(chat.messages[-1][1], "pong")
            await broker.command("teslabot/action_refresh_delay/set", "8")
            for _ in range(100):
                if self.mqtt.action_refresh_delay == 8:
                    break
                await self.clock.real_sleep(0)
            self.assertEqual(self.mqtt.action_refresh_delay, 8)
            self.assertIs(self.mqtt._session.jobs[self.id1], job)
            self.assertEqual(self.app.refresh_vehicle.await_count, 0)
            self.assertFalse(runtime.done())
            event.set()
            await asyncio.wait_for(job, 1)
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError): await runtime

    async def test_saved_integer_validation_and_storage_rollback_preserve_other_namespaces(self):
        section = "mqtt_action_refresh_delay"
        self.state[section] = {"other-namespace": "23", self.mqtt._manifest_key: "7"}
        self.mqtt.action_refresh_delay = 7
        for value in ("", "5.0", "false", "NaN", "Infinity", "-1", "301", " 5", "05", "５"):
            self.state[section][self.mqtt._manifest_key] = value
            with self.assertRaises(control.ConfigError): self.build()
        self.state[section][self.mqtt._manifest_key] = "7"
        self.state.save_to_storage.side_effect = OSError("failed storage detail")
        with self.assertLogs("teslabot.mqtt", "ERROR"):
            await self.setting("9")
        self.assertEqual(self.mqtt.action_refresh_delay, 7)
        self.assertEqual(dict(self.state[section].items()), {"other-namespace": "23", self.mqtt._manifest_key: "7"})
        self.assertEqual(self.broker.publications, [])

    async def test_pending_jobs_drain_before_client_exit_on_disconnect_and_shutdown(self):
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                self.app._get_vehicle_list.return_value = [{"display_name": "One"}]
                self.app._vehicle_id.return_value = self.id1
                reading, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                self.releases.append(release)
                async def read(*args, **kwargs):
                    reading.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleanup.set()
                        await release.wait()
                self.app.refresh_vehicle.side_effect = read
                class FailingBroker(Broker):
                    async def receive(self):
                        while True:
                            item = await self.incoming.get()
                            if isinstance(item, Exception):
                                raise item
                            yield item
                first, second = FailingBroker(), Broker()
                async def retry(delay):
                    pass
                self.mqtt._wait_retry = retry
                with self.clock.install(), mock.patch("aiomqtt.Client", side_effect=[first, second]):
                    runtime = self.start(self.mqtt.run())
                    await asyncio.wait_for(first.online.wait(), 1)
                    await first.command(f"teslabot/{self.id1}/ac/set")
                    (await self.clock.next()).set()
                    await asyncio.wait_for(reading.wait(), 1)
                    old = self.mqtt._session
                    if disconnect:
                        await first.incoming.put(aiomqtt.MqttError("disconnect detail"))
                    else:
                        runtime.cancel()
                    await asyncio.wait_for(cleanup.wait(), 1)
                    self.assertFalse(first.closed)
                    self.assertFalse(old.active)
                    self.assertTrue(old.jobs)
                    await self.clock.real_sleep(0.01)
                    self.assertFalse(first.closed)
                    release.set()
                    if disconnect:
                        await asyncio.wait_for(second.online.wait(), 1)
                        self.assertTrue(first.closed)
                        self.assertIsNot(self.mqtt._session, old)
                        self.assertEqual(self.mqtt._session.jobs, {})
                        runtime.cancel()
                    with self.assertRaises(asyncio.CancelledError): await runtime
                self.assertEqual(old.jobs, {})
                self.assertTrue(first.closed)

    async def test_runtime_setting_publish_failure_reconnects_with_committed_state(self):
        self.app._get_vehicle_list.return_value = []
        first, second = Broker(), Broker()
        original = first.publish
        async def publish(topic, payload, **kwargs):
            if topic.endswith("/action_refresh_delay/state") and payload == "19":
                raise aiomqtt.MqttError("committed publication detail")
            await original(topic, payload, **kwargs)
        first.publish = publish
        async def retry(delay):
            pass
        self.mqtt._wait_retry = retry
        with mock.patch("aiomqtt.Client", side_effect=[first, second]):
            runtime = self.start(self.mqtt.run())
            await asyncio.wait_for(first.online.wait(), 1)
            await first.command("teslabot/action_refresh_delay/set", "19")
            await asyncio.wait_for(second.online.wait(), 1)
            self.assertTrue(first.closed)
            self.assertEqual(second.retained["teslabot/action_refresh_delay/state"], "19")
            self.assertEqual(self.build().action_refresh_delay, 19)
            self.assertEqual(self.app.refresh_vehicle.await_count, 0)
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError): await runtime

    async def test_unauthorized_settings_cannot_drive_reconnection(self):
        self.app.authorized = False
        broker = Broker()
        with mock.patch("aiomqtt.Client", return_value=broker) as factory:
            runtime = self.start(self.mqtt.run())
            for _ in range(100):
                if self.mqtt._session is not None and self.mqtt._session.receiver is not None:
                    break
                await self.clock.real_sleep(0)
            await broker.command("teslabot/action_refresh_delay/set", "10")
            await self.clock.real_sleep(0.01)
            factory.assert_called_once()
            self.assertFalse(runtime.done())
            self.assertEqual(self.mqtt.action_refresh_delay, 5)
            self.assertEqual(broker.subscriptions, [])
            self.assertEqual(self.app.refresh_vehicle.await_count, 0)
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError): await runtime

    async def test_programming_fault_during_job_shutdown_is_not_lost(self):
        self.app._get_vehicle_list.return_value = [{"display_name": "One"}]
        self.app._vehicle_id.return_value = self.id1
        reading = asyncio.Event()
        async def read(*args, **kwargs):
            reading.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("cleanup programming detail")
        self.app.refresh_vehicle.side_effect = read
        broker = Broker()
        with self.clock.install(), mock.patch("aiomqtt.Client", return_value=broker), \
             self.assertLogs("teslabot.mqtt", "ERROR") as logs:
            runtime = self.start(self.mqtt.run())
            await asyncio.wait_for(broker.online.wait(), 1)
            await broker.command(f"teslabot/{self.id1}/ac/set")
            (await self.clock.next()).set()
            await asyncio.wait_for(reading.wait(), 1)
            runtime.cancel()
            with self.assertRaisesRegex(RuntimeError, "cleanup programming detail"):
                await runtime
            self.assertIn("cleanup programming detail", "\n".join(logs.output))
            self.assertTrue(broker.closed)
            self.assertEqual(self.mqtt._session.jobs, {})

    async def test_receiver_terminal_cleanup_on_auth_transition_waits_for_all_drain(self):
        self.app._get_vehicle_list.return_value = [{"display_name": "One"}]
        self.app._vehicle_id.return_value = self.id1
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        terminal = RuntimeError("receiver cleanup programming fault")
        async def action(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
                raise terminal
        self.app.set_ac.side_effect = action
        first, second = Broker(), Broker()
        with mock.patch("aiomqtt.Client", side_effect=[first, second]) as factory, \
             self.assertLogs("teslabot.mqtt", "ERROR") as logs:
            runtime = self.start(self.mqtt.run())
            await asyncio.wait_for(first.online.wait(), 1)
            old = self.mqtt._session
            assert old is not None
            await first.command(f"teslabot/{self.id1}/ac/set")
            await asyncio.wait_for(entered.wait(), 1)
            self.app.auth_generation += 1
            self.mqtt._auth_event.set()
            await asyncio.wait_for(cleanup.wait(), 1)
            self.assertFalse(runtime.done())
            self.assertFalse(first.closed)
            release.set()
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(runtime, 1)
            self.assertIs(caught.exception, terminal)
            self.assertIs(old.error, terminal)
            self.assertIn("receiver cleanup programming fault", "\n".join(logs.output))
            self.assertIn("owned task", "\n".join(logs.output))
            self.assertIn("Traceback", "\n".join(logs.output))
            self.assertTrue(first.closed)
            self.assertEqual(old.jobs, {})
            assert old.receiver is not None
            self.assertTrue(old.receiver.done())
            factory.assert_called_once()
            self.assertFalse(second.online.is_set())

    async def mixed_teardown_failure(self, terminal_first):
        self.app._get_vehicle_list.return_value = [{"display_name": "One"}, {"display_name": "Two"}]
        self.app._vehicle_id.side_effect = lambda vehicle: self.id1 if vehicle["display_name"] == "One" else self.id2
        reading, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        terminal = RuntimeError("second vehicle cleanup fault" if not terminal_first else "first vehicle programming fault")
        transport = aiomqtt.MqttError("first vehicle broker fault" if not terminal_first else "second vehicle cleanup broker fault")
        async def read(*args, **kwargs):
            if kwargs["vehicle_id"] == self.id2:
                reading.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.set()
                    await release.wait()
                    raise transport if terminal_first else terminal
            if terminal_first:
                raise terminal
            return snapshot(self.id1)
        self.app.refresh_vehicle.side_effect = read
        first, second = Broker(), Broker()
        if not terminal_first:
            first.publish_error = transport
        with self.clock.install(), mock.patch("aiomqtt.Client", side_effect=[first, second]) as factory, \
             self.assertLogs("teslabot.mqtt", "ERROR") as logs:
            runtime = self.start(self.mqtt.run())
            await asyncio.wait_for(first.online.wait(), 1)
            old = self.mqtt._session
            assert old is not None
            await first.command(f"teslabot/{self.id2}/ac/set")
            (await self.clock.next()).set()
            await asyncio.wait_for(reading.wait(), 1)
            await first.command(f"teslabot/{self.id1}/ac/set")
            (await self.clock.next()).set()
            await asyncio.wait_for(cleanup.wait(), 1)
            self.assertFalse(runtime.done())
            self.assertFalse(first.closed)
            release.set()
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(runtime, 1)
            self.assertIs(caught.exception, terminal)
            self.assertIs(old.error, terminal)
            text = "\n".join(logs.output)
            self.assertIn(str(terminal), text)
            self.assertIn(str(transport), text)
            self.assertIn("Traceback", text)
            self.assertTrue(first.closed)
            self.assertEqual(old.jobs, {})
            self.assertEqual(old.cancelling, set())
            assert old.receiver is not None
            self.assertTrue(old.receiver.done())
            factory.assert_called_once()
            self.assertFalse(second.online.is_set())

    async def test_recoverable_first_terminal_second_during_job_drain_is_terminal(self):
        await self.mixed_teardown_failure(terminal_first=False)

    async def test_terminal_first_recoverable_second_cannot_demote_session_failure(self):
        await self.mixed_teardown_failure(terminal_first=True)

    async def test_auth_transition_receiver_cancellation_without_fault_reconnects_normally(self):
        self.app._get_vehicle_list.return_value = [{"display_name": "One"}]
        self.app._vehicle_id.return_value = self.id1
        entered = asyncio.Event()
        async def action(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.app.set_ac.side_effect = action
        first, second = Broker(), Broker()
        with mock.patch("aiomqtt.Client", side_effect=[first, second]):
            runtime = self.start(self.mqtt.run())
            await asyncio.wait_for(first.online.wait(), 1)
            old = self.mqtt._session
            assert old is not None
            await first.command(f"teslabot/{self.id1}/ac/set")
            await asyncio.wait_for(entered.wait(), 1)
            self.app.auth_generation += 1
            self.mqtt._auth_event.set()
            await asyncio.wait_for(second.online.wait(), 1)
            self.assertIsNone(old.error)
            self.assertTrue(first.closed)
            self.assertFalse(runtime.done())
            assert old.receiver is not None
            self.assertTrue(old.receiver.cancelled())
            runtime.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runtime

    async def test_session_drain_records_every_receiver_and_job_outcome_with_terminal_priority(self):
        transport = aiomqtt.MqttError("transport task outcome")
        first_terminal = RuntimeError("terminal task outcome")
        receiver_terminal = ValueError("receiver task outcome")
        async def fail(exn):
            raise exn
        tasks = [self.start(fail(exn)) for exn in (transport, first_terminal, receiver_terminal)]
        await asyncio.wait(tasks)
        self.session.jobs = {self.id1: tasks[0], self.id2: tasks[1]}
        self.session.receiver = tasks[2]
        with self.assertLogs("teslabot.mqtt", "ERROR") as logs:
            await self.mqtt._stop_session(self.session)
        self.assertIs(self.session.error, first_terminal)
        text = "\n".join(logs.output)
        for exn in (transport, first_terminal, receiver_terminal):
            self.assertIn(str(exn), text)
        self.assertGreaterEqual(text.count("Traceback"), 3)
        self.assertEqual(self.session.jobs, {})


class RealSDKDelayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        self.fixture = sdk_tests.SDKBoundaryTests()
        self.fixture.setUp()
        self.fixture.retry_sleep.stop()
        self.app, self.http = self.fixture.app, self.fixture.http
        self.mqtt = MqttControl(Env(Config("unused", {"mqtt": {"host": "localhost"}}), self.fixture.env.state))
        self.mqtt.set_app(self.app)
        self.mqtt.vehicles = {self.fixture.identity: "Synthetic car"}
        self.mqtt._generation = self.app.auth_generation
        self.session = _Session(self.fixture.client, self.app.auth_generation)
        self.mqtt._session = self.session
        self.clock = Clock()
        self.tasks = []
        self.releases = []

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.mqtt.close()
        await self.fixture.mqtt.close()
        await self.app.close()

    async def handle(self, operation="ac", payload="ON"):
        await self.mqtt._handle(self.fixture.client, f"teslabot/{self.fixture.identity}/{operation}/set", payload, False)

    async def test_real_sdk_settling_delay_publishes_one_later_observation_all_actions(self):
        self.assertEqual(self.mqtt.action_refresh_delay, 5)
        with self.clock.install():
            for operation, payload in (("ac", "ON"), ("ac", "OFF"), ("sauna", "ON"), ("sauna", "OFF"), ("charge_limit", "70")):
                self.fixture.publications.clear()
                self.http.telemetry["climate_state"]["is_climate_on"] = False
                reads = self.http.data_reads
                await self.handle(operation, payload)
                event = await self.clock.next()
                self.assertEqual(self.http.data_reads, reads)
                self.assertFalse(self.app._operation_lock.locked())
                self.assertEqual(len(self.fixture.publications), 1)
                self.assertTrue(self.fixture.publications[0][1]["success"])
                self.assertGreater(self.clock.waits[-1][0], 4.9)
                self.http.telemetry["climate_state"]["is_climate_on"] = True
                self.http.telemetry["charge_state"]["charge_limit_soc"] = 83
                job = self.session.jobs[self.fixture.identity]
                event.set()
                await asyncio.wait_for(job, 1)
                self.assertEqual(self.http.data_reads, reads + 1)
                self.assertEqual(len(self.fixture.publications), 2)
                self.assertTrue(self.fixture.publications[-1][1]["climate_on"])
                self.assertEqual(self.fixture.publications[-1][1]["charge_limit"], 83)
                self.assertNotIn("data", self.fixture.publications[-1][1])
        self.fixture.assert_worker_requests()

    async def test_manual_refresh_supersedes_job_waiting_for_real_app_gate(self):
        with self.clock.install():
            await self.handle()
            event = await self.clock.next()
            await self.app._operation_lock.acquire()
            try:
                job = self.session.jobs[self.fixture.identity]
                event.set()
                await self.clock.real_sleep(0.01)
                self.assertEqual(self.http.data_reads, 0)
                manual = asyncio.create_task(self.handle("refresh", ""))
                self.tasks.append(manual)
                await self.clock.real_sleep(0.01)
                self.assertTrue(job.cancelled())
                self.assertFalse(manual.done())
                self.assertEqual(self.http.data_reads, 0)
            finally:
                self.app._operation_lock.release()
            await asyncio.wait_for(manual, 1)
        self.assertEqual(self.http.data_reads, 1)
        self.fixture.assert_worker_requests()

    async def test_manual_refresh_drains_real_http_thread_before_new_operation(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.http.send
        first = True
        def send(request, **kwargs):
            nonlocal first
            if "vehicle_data" in request.url and first:
                first = False
                entered.set()
                release.wait(2)
            return original(request, **kwargs)
        self.http.send = send
        with self.clock.install():
            await self.handle()
            (await self.clock.next()).set()
            while not entered.is_set():
                await self.clock.real_sleep(0.001)
            job = self.session.jobs[self.fixture.identity]
            manual = asyncio.create_task(self.handle("refresh", ""))
            self.tasks.append(manual)
            await self.clock.real_sleep(0.01)
            self.assertFalse(job.done())
            self.assertFalse(manual.done())
            self.assertTrue(self.app._operation_lock.locked())
            release.set()
            await asyncio.wait_for(manual, 1)
        self.assertTrue(job.cancelled())
        states = [call for call in self.fixture.publications if call[0].endswith("/state")]
        self.assertEqual(len(states), 1)
        self.assertEqual(self.http.data_reads, 2)  # sent old read drains, only manual publishes
        self.fixture.assert_worker_requests()

    async def test_chat_actions_do_not_schedule_or_publish_mqtt(self):
        await self.app._command_climate(control.CommandContext(False, self.fixture.chat), ((True, None), ()))
        self.assertEqual(self.fixture.publications, [])
        self.assertEqual(self.session.jobs, {})
        self.assertEqual(self.http.data_reads, 0)
        self.fixture.assert_worker_requests()
