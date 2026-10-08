import asyncio
import json
import unittest
from typing import Any

from teslabot import tesla
import tests.test_action_refresh_delay as delay


SEATS = {
    "seat_heater_left": 0,
    "seat_heater_right": 1,
    "seat_heater_rear_left": 2,
    "seat_heater_rear_center": 4,
    "seat_heater_rear_right": 5,
}


class HeaterControlTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self) -> None:
        self.fixture = delay.RealSDKDelayTests()
        self.fixture.setUp()
        self.app, self.mqtt, self.http = self.fixture.app, self.fixture.mqtt, self.fixture.http
        self.identity = self.fixture.fixture.identity

    async def asyncTearDown(self) -> None:
        await self.fixture.asyncTearDown()

    async def test_every_seat_level_and_steering_state_use_sdk_and_delayed_observation(self) -> None:
        cases = [(seat, str(level), "remote_seat_heater_request", {"heater": index, "level": level})
                 for seat, index in SEATS.items() for level in range(4)]
        cases += [("steering_wheel_heater", payload, "remote_steering_wheel_heater_request", {"on": enabled})
                  for payload, enabled in (("ON", True), ("OFF", False))]
        with self.fixture.clock.install():
            for seat, payload, command, body in cases:
                with self.subTest(seat=seat, payload=payload):
                    reads = self.http.data_reads
                    publications = self.fixture.fixture.publications
                    publications.clear()
                    await self.fixture.handle(seat, payload)
                    event = await self.fixture.clock.next()
                    self.assertEqual(self.http.data_reads, reads)
                    self.assertEqual(len(publications), 1)
                    result = publications[0][1]
                    self.assertTrue(result["success"])
                    self.assertEqual(result["vehicle_id"], self.identity)
                    self.assertEqual(result["action"], seat)
                    self.assertEqual(result["requested_value"], int(payload) if seat in SEATS else payload == "ON")
                    path, _, _, _, request = self.http.calls[-1]
                    self.assertEqual(path, "/api/1/vehicles/1/command/" + command)
                    self.assertEqual(json.loads(request), body)
                    self.assertFalse(self.app._operation_lock.locked())
                    self.assertGreater(self.fixture.clock.waits[-1][0], 4.9)
                    # Deliberately disagree with the request to prove that state is observed.
                    observed = (int(payload) + 1) % 4 if seat in SEATS else payload != "ON"
                    self.http.telemetry["climate_state"][seat] = observed
                    job = self.fixture.session.jobs[self.identity]
                    event.set()
                    await asyncio.wait_for(job, 1)
                    self.assertEqual(self.http.data_reads, reads + 1)
                    self.assertEqual(len(publications), 2)
                    self.assertEqual(publications[-1][1][seat], observed)
        self.fixture.fixture.assert_worker_requests()

    async def test_failed_heaters_refresh_in_delayed_and_zero_modes(self) -> None:
        with self.fixture.clock.install():
            for seconds in (5, 0):
                self.mqtt.action_refresh_delay = seconds
                for operation, payload in (("seat_heater_rear_center", "2"), ("steering_wheel_heater", "OFF")):
                    for failure in ("rejected", "http"):
                        self.http.command_result = failure != "rejected"
                        self.http.command_status = 400 if failure == "http" else 200
                        reads = self.http.data_reads
                        publications = self.fixture.fixture.publications
                        publications.clear()
                        await self.fixture.handle(operation, payload)
                        self.assertFalse(publications[0][1]["success"])
                        self.assertIn("command rejection detail" if failure == "rejected" else "HTTP command failure detail",
                                      publications[0][1]["error"])
                        if seconds:
                            event = await self.fixture.clock.next()
                            self.assertEqual(self.http.data_reads, reads)
                            job = self.fixture.session.jobs[self.identity]
                            event.set()
                            await asyncio.wait_for(job, 1)
                        self.assertEqual(self.http.data_reads, reads + 1)
                        self.assertEqual(len(publications), 2)

    async def test_invalid_retained_and_wrong_topics_do_not_supersede_pending_refresh(self) -> None:
        with self.fixture.clock.install():
            await self.fixture.handle("seat_heater_left", "3")
            event = await self.fixture.clock.next()
            job = self.fixture.session.jobs[self.identity]
            calls = len(self.http.calls)
            for seat in SEATS:
                for payload in ("", "ON", "-1", "4", "1.0", "01", " 1", "1\n", "\u0661"):
                    await self.fixture.handle(seat, payload)
                    self.assertFalse(self.fixture.fixture.publications[-1][1]["success"])
                    self.assertIs(self.fixture.session.jobs[self.identity], job)
            for payload in ("1", "true", "on", "", "OFF "):
                await self.fixture.handle("steering_wheel_heater", payload)
                self.assertFalse(self.fixture.fixture.publications[-1][1]["success"])
            before = len(self.fixture.fixture.publications)
            for topic, retained in ((f"teslabot/{self.identity}/seat_heater_left/set", True),
                                    (f"teslabot/{self.identity}/steering_wheel_heater/set", True),
                                    (f"teslabot/{self.identity}/seat_heater_left/get", False),
                                    (f"teslabot/{self.identity}/seat_heater_unknown/set", False),
                                    ("teslabot/unknown/seat_heater_left/set", False)):
                await self.mqtt._handle(self.fixture.fixture.client, topic, "3", retained)
            self.assertEqual(len(self.fixture.fixture.publications), before)
            self.assertEqual(len(self.http.calls), calls)
            await self.fixture.handle("steering_wheel_heater", "ON")
            replacement = await self.fixture.clock.next()
            self.assertTrue(job.cancelled())
            event.set()
            self.assertEqual(self.http.data_reads, 0)
            latest = self.fixture.session.jobs[self.identity]
            replacement.set()
            await asyncio.wait_for(latest, 1)
            self.assertEqual(self.http.data_reads, 1)

    async def test_app_validates_heater_values_before_sdk_calls(self) -> None:
        # Deliberately bypass static input contracts to exercise runtime validation.
        level: Any
        for seat, level in (("unknown", 1), ("seat_heater_left", True), ("seat_heater_right", "2"),
                            ("seat_heater_left", -1), ("seat_heater_left", 4), ("seat_heater_left", 1.0)):
            with self.assertRaises(tesla.ArgException):
                await self.app.set_seat_heater(None, seat, level)
        value: Any
        for value in (0, 1, "ON", None):
            with self.assertRaises(tesla.ArgException):
                await self.app.set_steering_wheel_heater(None, value)
        self.assertEqual(self.http.calls, [])

    async def test_steering_observation_preserves_false_and_resets_invalid_or_missing(self) -> None:
        value: object
        for value in (True, False, None, 0, 1, "false", [], {}):
            self.http.telemetry["climate_state"]["steering_wheel_heater"] = value
            snapshot = await self.app.refresh_vehicle(None)
            self.assertIs(snapshot.steering_wheel_heater, value if type(value) is bool else None)
            await self.mqtt._publish_snapshot(self.fixture.fixture.client, snapshot)
            self.assertIs(self.fixture.fixture.publications[-1][1]["steering_wheel_heater"], snapshot.steering_wheel_heater)
        self.http.telemetry.pop("climate_state")
        self.assertIsNone((await self.app.refresh_vehicle(None)).steering_wheel_heater)

    async def test_discovery_migrates_old_seat_sensors_in_owned_namespace(self) -> None:
        fixture = delay.DelayTests()
        fixture.setUp()
        try:
            mqtt, app, broker = fixture.mqtt, fixture.app, fixture.broker
            mqtt.prefix = "custom/prefix"
            mqtt._entity_prefix = "teslabot_custom"
            app._get_vehicle_list.return_value = [{"display_name": "One"}]
            app._vehicle_id.return_value = fixture.id1
            for seat in SEATS:
                broker.retained[mqtt._config_topic(fixture.id1, "sensor/" + seat)] = "old sensor"
            await mqtt._reconcile(broker, 0)
            for seat in SEATS:
                old = mqtt._config_topic(fixture.id1, "sensor/" + seat)
                new = mqtt._config_topic(fixture.id1, "number/" + seat)
                self.assertEqual(broker.retained[old], "")
                config = json.loads(broker.retained[new])
                self.assertEqual((config["min"], config["max"], config["step"]), (0, 3, 1))
                self.assertTrue(config["optimistic"])
                self.assertEqual(config["command_topic"], f"custom/prefix/{fixture.id1}/{seat}/set")
                self.assertEqual(config["state_topic"], f"custom/prefix/{fixture.id1}/state")
                self.assertEqual(config["unique_id"], f"teslabot_custom_{fixture.id1}_{seat}")
                self.assertLess(next(i for i, call in enumerate(broker.publications) if call[0] == old),
                                next(i for i, call in enumerate(broker.publications) if call[0] == new))
            steering = json.loads(broker.retained[mqtt._config_topic(fixture.id1, "switch/steering_wheel_heater")])
            self.assertTrue(steering["optimistic"])
            self.assertEqual(steering["command_topic"], f"custom/prefix/{fixture.id1}/steering_wheel_heater/set")
            self.assertIn(f"custom/prefix/{fixture.id1}/+/set", broker.subscriptions)
        finally:
            await fixture.asyncTearDown()
