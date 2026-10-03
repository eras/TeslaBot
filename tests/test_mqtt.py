import datetime
import asyncio
import json
import unittest
from unittest import mock

try:
    import jinja2
except ImportError:
    jinja2 = None

from teslabot.config import Config
from teslabot.env import Env
from teslabot.filestate import FileState
from teslabot.mqtt import MqttControl, _Session
from teslabot.tesla import ActionResult, VehicleSnapshot


class TestMqtt(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        # Older tests in this project use get_event_loop() directly.
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self) -> None:
        config = Config("test.ini", {"mqtt": {"host": "localhost", "action_refresh_delay": "0"}})
        self.control = MqttControl(Env(config, FileState("test-state.ini")))
        self.control.vehicles = {"id1": "Test vehicle"}
        self.client = mock.AsyncMock()
        self.app = mock.Mock()
        self.app.auth_events = []
        self.app.auth_generation = 0
        self.app.authorized = True
        self.app.refresh_vehicle = mock.AsyncMock()
        self.app.set_ac = mock.AsyncMock()
        self.app.set_sauna = mock.AsyncMock()
        self.app.set_charge_limit = mock.AsyncMock()
        self.control.set_app(self.app)
        self.control._generation = 0
        self.control._session = _Session(self.client, 0)
        self.snapshot = VehicleSnapshot(
            vehicle_id="id1", display_name="Test vehicle",
            observed_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            battery_level=80, charging_state="Disconnected", charge_limit=90,
            charge_amps=16, climate_on=False, defrost_mode=None,
            inside_temp=18.0, outside_temp=5.0, temperature_unit="C", data={"private": "not published"},
        )
        self.app.refresh_vehicle.return_value = self.snapshot

    async def test_refresh_publishes_observed_state_only_when_requested(self) -> None:
        self.client.publish.assert_not_awaited()
        await self.control._handle(self.client, "teslabot/id1/refresh/set", "", False)
        args, kwargs = self.client.publish.await_args
        self.assertEqual(args[0], "teslabot/id1/state")
        self.assertEqual(json.loads(args[1])["charge_limit"], 90)
        self.assertNotIn("private", args[1])
        self.assertTrue(kwargs["retain"])

    async def test_rejects_retained_unknown_and_unrestricted_commands(self) -> None:
        for topic, retained in (("teslabot/id1/ac/set", True),
                                ("teslabot/id1/authorize/set", False),
                                ("teslabot/unknown/ac/set", False),
                                ("teslabot/id1/ac/set/extra", False)):
            await self.control._handle(self.client, topic, "ON", retained)
        self.client.publish.assert_not_awaited()
        self.app.set_ac.assert_not_awaited()

    async def test_action_publishes_result_then_fresh_observed_state(self) -> None:
        self.app.set_charge_limit.return_value = ActionResult("id1", "charge_limit", 70, True)
        await self.control._handle(self.client, "teslabot/id1/charge_limit/set", "70", False)
        self.app.set_charge_limit.assert_awaited_once_with(None, 70, vehicle_id="id1")
        self.assertEqual(self.client.publish.await_count, 2)
        self.assertEqual(self.client.publish.await_args_list[0].args[0], "teslabot/id1/result")
        self.assertEqual(self.client.publish.await_args_list[1].args[0], "teslabot/id1/state")

    async def test_invalid_limit_and_failed_action_do_not_refresh(self) -> None:
        await self.control._handle(self.client, "teslabot/id1/charge_limit/set", "101", False)
        self.app.set_charge_limit.assert_not_awaited()
        self.app.refresh_vehicle.assert_not_awaited()
        self.assertFalse(json.loads(self.client.publish.await_args.args[1])["success"])
        self.client.publish.reset_mock()
        self.app.set_ac.return_value = ActionResult("id1", "ac", True, False, "rejected")
        await self.control._handle(self.client, "teslabot/id1/ac/set", "ON", False)
        self.app.refresh_vehicle.assert_not_awaited()
        self.assertEqual(self.client.publish.await_count, 1)

    async def test_followup_read_failure_does_not_publish_requested_state(self) -> None:
        self.app.set_ac.return_value = ActionResult("id1", "ac", True, True)
        from teslabot.tesla import AppException
        self.app.refresh_vehicle.side_effect = AppException("offline")
        await self.control._handle(self.client, "teslabot/id1/ac/set", "ON", False)
        self.assertEqual(self.client.publish.await_count, 1)
        self.assertEqual(self.client.publish.await_args.args[0], "teslabot/id1/result")

    def test_discovery_uses_buttons_for_sauna(self) -> None:
        entities = self.control._discovery("id1", "Test vehicle")
        self.assertIn("button/sauna_on", entities)
        self.assertIn("button/sauna_off", entities)
        self.assertNotIn("switch/sauna", entities)
        self.assertTrue(all(entity["availability_topic"] == "teslabot/availability"
                            for entity in entities.values()))
        self.assertIn("is sameas false", entities["switch/ac"]["value_template"])
        self.assertIn("else 'None'", entities["switch/ac"]["value_template"])

    def test_ac_template_resets_unknown_state(self) -> None:
        if jinja2 is None:
            self.skipTest("Jinja2 is needed to render HA templates")
        template = jinja2.Environment().from_string(
            self.control._discovery("id1", "Test vehicle")["switch/ac"]["value_template"])
        observed = [True, None, False, None, True, {}]
        rendered = [template.render(value_json={"climate_on": value} if not isinstance(value, dict) else value)
                    for value in observed]
        self.assertEqual(rendered, ["ON", "None", "OFF", "None", "ON", "None"])

    async def test_failed_selection_uses_requested_id_without_refresh(self) -> None:
        self.app.set_ac.return_value = ActionResult("id1", "ac", True, False, "Vehicle identity not found")
        await self.control._handle(self.client, "teslabot/id1/ac/set", "ON", False)
        self.app.set_ac.assert_awaited_once_with(None, True, vehicle_id="id1")
        self.assertEqual(json.loads(self.client.publish.await_args.args[1])["vehicle_id"], "id1")
        self.app.refresh_vehicle.assert_not_awaited()

    async def test_connection_discovery_and_graceful_shutdown(self) -> None:
        online = asyncio.Event()

        class FakeClient:
            def __init__(self):
                self.publications = []
                self.messages = self.receive()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def publish(self, topic, payload, **kwargs):
                self.publications.append((topic, payload))
                if topic == "teslabot/availability" and payload == "online":
                    online.set()

            async def subscribe(self, topic, **kwargs):
                pass

            async def receive(self):
                await asyncio.Event().wait()
                yield None

        client = FakeClient()
        self.app._get_vehicle_list = mock.AsyncMock(return_value=[{"display_name": "Test vehicle"}])
        self.app._vehicle_id.return_value = "0123456789abcdef"
        self.control._state.save = mock.AsyncMock()
        with mock.patch("aiomqtt.Client", return_value=client):
            task = asyncio.create_task(self.control.run())
            await asyncio.wait_for(online.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(client.publications[-1], ("teslabot/availability", "offline"))
        configs = [c for c in client.publications if c[0].endswith("/config")]
        self.assertEqual(len(configs), len(self.control._discovery("id1", "Test vehicle")) + 2)
        self.assertIn("homeassistant/sensor/teslabot/version/config", [topic for topic, _ in configs])
        self.app.refresh_vehicle.assert_not_awaited()
