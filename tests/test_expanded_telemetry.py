import asyncio
import dataclasses
import datetime
import json
import unittest
from unittest import mock

try:
    import jinja2
except ImportError:
    jinja2 = None

from teslabot import __version__, control, tesla
import tests.test_action_refresh_delay as delay
import tests.test_action_refresh_timestamp as timestamp
import tests.test_sdk_boundary as sdk


SEATS = ("seat_heater_left", "seat_heater_right", "seat_heater_rear_left",
         "seat_heater_rear_center", "seat_heater_rear_right")
WHEELS = ("tpms_pressure_fl", "tpms_pressure_fr", "tpms_pressure_rl", "tpms_pressure_rr")
OPENINGS = {
    "df": "door_driver_front_open", "dr": "door_driver_rear_open",
    "pf": "door_passenger_front_open", "pr": "door_passenger_rear_open",
    "fd_window": "window_driver_front_open", "rd_window": "window_driver_rear_open",
    "fp_window": "window_passenger_front_open", "rp_window": "window_passenger_rear_open",
    "ft": "frunk_open", "rt": "trunk_open",
}
TEXT = ("software_update_status", "software_update_version", "car_version")
SCALARS = set(SEATS + WHEELS + TEXT) | {
    "inside_temp", "outside_temp", "charge_amps", "charger_power_kw", "charge_rate_kmh",
    "charge_finish_eta", "odometer_km", "software_update_download_percent",
    "software_update_install_percent", "software_update_expected_duration_s",
}
BINARIES = set(OPENINGS.values()) | {"locked", "charge_port_door_open"}
LEGACY_ENTITIES = {"sensor/battery", "sensor/charging", "sensor/last_refresh", "switch/ac",
                   "number/charge_limit", "button/refresh", "button/sauna_on", "button/sauna_off"}


def telemetry():
    data = sdk.telemetry()
    data["climate_state"].update(dict(zip(SEATS, (0, 1, 2, 3, 1))))
    data["climate_state"].update(inside_temp=-2.5, outside_temp=0)
    data["charge_state"].update(charger_power=7.5, charge_rate=12.25, charging_state="Charging",
                                minutes_to_full_charge=12.5, time_to_full_charge=2,
                                charge_port_door_open=False)
    data["vehicle_state"].update(dict(zip(WHEELS, (0, 2.1, 2.2, 2.3))))
    data["vehicle_state"].update(df=0, dr=2, pf=1, pr=0, fd_window=0, fp_window=2,
                                rd_window=1, rp_window=0, ft=0, rt=1, odometer=123.125,
                                car_version=" synthetic-firmware full-hash ", locked=True,
                                software_update={"status": " downloading ", "version": " synthetic-update ",
                                                 "download_perc": 0, "install_perc": 12.5,
                                                 "expected_duration_sec": 90.5})
    data["diagnostics"]["synthetic_private_id"] = "NOT-A-REAL-DEVICE-ID"
    return data


class ExpandedTelemetryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        self.fixture = delay.RealSDKDelayTests()
        self.fixture.setUp()
        self.app, self.http, self.mqtt = self.fixture.app, self.fixture.http, self.fixture.mqtt
        self.client = self.fixture.fixture.client
        self.topic = f"teslabot/{self.fixture.fixture.identity}/state"
        self.http.telemetry = telemetry()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def observe(self):
        reads = self.http.data_reads
        result = await self.app.refresh_vehicle(None)
        self.assertEqual(self.http.data_reads, reads + 1)
        calls = len(self.http.calls)
        await self.mqtt._publish_snapshot(self.client, result)
        self.assertEqual(len(self.http.calls), calls)
        self.fixture.fixture.assert_worker_requests()
        state = json.loads(self.fixture.fixture.retained[self.topic])
        json.dumps(state, allow_nan=False)
        return result, state

    async def test_all_groups_real_sdk_detached_explicit_projection_and_fixed_api_units(self):
        snapshots = []
        for distance, temperature in (("km/hr", "C"), ("mi/hr", "F")):
            self.http.telemetry["gui_settings"].update(gui_distance_units=distance, gui_temperature_units=temperature)
            result, state = await self.observe()
            snapshots.append(result)
            self.assertEqual(state["inside_temp"], -2.5)
            self.assertEqual(state["outside_temp"], 0)
            self.assertEqual(state["temperature_unit"], "C")
            self.assertEqual(state["charge_amps"], 16)
            self.assertEqual(state["charger_power_kw"], 7.5)
            self.assertAlmostEqual(state["charge_rate_kmh"], 12.25 * 1.609344)
            self.assertAlmostEqual(state["odometer_km"], 123.125 * 1.609344)
            self.assertEqual([state[key] for key in SEATS], [0, 1, 2, 3, 1])
            self.assertEqual([state[key] for key in WHEELS], [0, 2.1, 2.2, 2.3])
            self.assertEqual(state["software_update_status"], "downloading")
            self.assertEqual(state["software_update_version"], "synthetic-update")
            self.assertEqual(state["car_version"], "synthetic-firmware full-hash")
            self.assertEqual(state["software_update_download_percent"], 0)
            self.assertEqual(state["software_update_install_percent"], 12.5)
            self.assertEqual(state["software_update_expected_duration_s"], 90.5)
            self.assertTrue(state["locked"])
            self.assertFalse(state["charge_port_door_open"])
            for source, key in OPENINGS.items():
                self.assertIs(state[key], self.http.telemetry["vehicle_state"][source] > 0, key)
            self.assertEqual(datetime.datetime.fromisoformat(state["charge_finish_eta"]),
                             result.observed_at + datetime.timedelta(minutes=12.5))
            self.assertEqual(state["observed_at"], result.observed_at.isoformat())
            self.assertEqual(set(state), {field.name for field in dataclasses.fields(result)} - {"data"})
            self.assertNotIn("diagnostics", state)
            self.assertNotIn("vin", state)
            self.assertNotIn("NOT-A-REAL-DEVICE-ID", json.dumps(state))
            self.assertEqual(self.fixture.fixture.publications[-1][2], {"qos": 1, "retain": True})
        prior = dataclasses.asdict(snapshots[0])
        self.http.telemetry["vehicle_state"]["software_update"]["status"] = "installing"
        self.http.telemetry["climate_state"]["seat_heater_left"] = 3
        for vehicle in self.fixture.fixture.vehicles:
            vehicle["vehicle_state"]["software_update"]["version"] = "mutated SDK cache"
        await self.observe()
        self.assertEqual(dataclasses.asdict(snapshots[0]), prior)
        self.assertEqual(self.http.commands, 0)

    async def test_each_seat_and_opening_routes_independently_with_exact_integer_domains(self):
        for source in SEATS:
            self.http.telemetry["climate_state"].update({key: 0 for key in SEATS})
            self.http.telemetry["climate_state"][source] = 3
            _, state = await self.observe()
            self.assertEqual({key: state[key] for key in SEATS}, {key: 3 if key == source else 0 for key in SEATS})
        for source, target in OPENINGS.items():
            self.http.telemetry["vehicle_state"].update({key: 0 for key in OPENINGS})
            self.http.telemetry["vehicle_state"][source] = 7
            _, state = await self.observe()
            self.assertEqual({key: state[key] for key in OPENINGS.values()},
                             {key: key == target for key in OPENINGS.values()})
        for invalid in (None, True, False, "0", -1, 1.0, float("inf"), float("nan")):
            self.http.telemetry["climate_state"].update({key: invalid for key in SEATS})
            self.http.telemetry["vehicle_state"].update({key: invalid for key in OPENINGS})
            _, state = await self.observe()
            for key in SEATS + tuple(OPENINGS.values()):
                self.assertIsNone(state[key], key)
        self.http.telemetry["climate_state"].update({key: 4 for key in SEATS})
        _, state = await self.observe()
        self.assertTrue(all(state[key] is None for key in SEATS))

    async def test_continuous_numeric_domains_preserve_zero_and_reject_bad_types_and_overflow(self):
        for value in (0, 0.125, None, -1, True, False, "2.5", float("nan"), float("inf"), -float("inf"), 10 ** 400):
            self.http.telemetry["vehicle_state"].update({key: value for key in WHEELS})
            self.http.telemetry["vehicle_state"]["odometer"] = value
            self.http.telemetry["charge_state"].update(charger_power=value, charge_rate=value)
            self.http.telemetry["vehicle_state"]["software_update"].update(
                download_perc=value, install_perc=value, expected_duration_sec=value)
            _, state = await self.observe()
            valid = type(value) in (int, float) and value in (0, 0.125)
            for key in WHEELS + ("charger_power_kw", "software_update_download_percent",
                                  "software_update_install_percent", "software_update_expected_duration_s"):
                self.assertEqual(state[key], value if valid else None, key)
            for key in ("odometer_km", "charge_rate_kmh"):
                self.assertEqual(state[key], value * 1.609344 if valid else None, key)
        self.http.telemetry["vehicle_state"]["odometer"] = 1.7e308
        self.http.telemetry["charge_state"]["charge_rate"] = 1.7e308
        self.http.telemetry["vehicle_state"]["software_update"].update(download_perc=100, install_perc=100.1)
        _, state = await self.observe()
        self.assertIsNone(state["odometer_km"])
        self.assertIsNone(state["charge_rate_kmh"])
        self.assertEqual(state["software_update_download_percent"], 100)
        self.assertIsNone(state["software_update_install_percent"])

    async def test_eta_uses_same_utc_observation_minutes_then_hours_and_handles_overflow(self):
        clock = timestamp.ObservationClock
        clock.current = datetime.datetime(2026, 10, 3, 12, 34, 56, 123456, tzinfo=datetime.timezone.utc)
        for minutes, hours, expected in (
            (1.25, 2, 75), (None, 0.125, 450), (0, 2, 7200), (-1, 1, 3600),
            (True, 1, 3600), ("3", 1, 3600), (float("nan"), 1, 3600),
            (float("inf"), 1, 3600), (None, None, None), (0, 0, None),
            (None, False, None), (None, "1", None), (None, float("inf"), None),
            (1e308, 1, None), (None, 1e308, None), (10 ** 400, 0, None),
        ):
            with self.subTest(minutes=minutes, hours=hours), mock.patch.object(tesla.datetime, "datetime", clock):
                self.http.telemetry["charge_state"].update(minutes_to_full_charge=minutes, time_to_full_charge=hours)
                result, state = await self.observe()
                self.assertEqual(state["charge_finish_eta"],
                                 (result.observed_at + datetime.timedelta(seconds=expected)).isoformat() if expected else None)
                self.assertEqual(result.observed_at, clock.current)
        self.http.telemetry["charge_state"].update(minutes_to_full_charge=1, time_to_full_charge=1)
        for charging in (None, "Disconnected", "Complete", "Stopped", "Starting", True, "charging"):
            self.http.telemetry["charge_state"]["charging_state"] = charging
            _, state = await self.observe()
            self.assertIsNone(state["charge_finish_eta"])
        self.http.telemetry["charge_state"]["charging_state"] = "Charging"
        clock.current = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        with mock.patch.object(tesla.datetime, "datetime", clock):
            _, state = await self.observe()
            self.assertIsNone(state["charge_finish_eta"])

    async def test_missing_null_nonmapping_sections_reset_only_unsupported_values(self):
        for section in ("climate_state", "charge_state", "vehicle_state"):
            for value in ("missing", None, [], False, 1, "not a mapping"):
                self.http.telemetry = telemetry()
                if value == "missing":
                    self.http.telemetry.pop(section)
                else:
                    self.http.telemetry[section] = value
                _, state = await self.observe()
                fields = SEATS + ("inside_temp", "outside_temp") if section == "climate_state" else (
                    ("charge_amps", "charger_power_kw", "charge_rate_kmh", "charge_finish_eta", "charge_port_door_open")
                    if section == "charge_state" else WHEELS + TEXT + tuple(OPENINGS.values()) + ("odometer_km", "locked"))
                for key in fields:
                    self.assertIsNone(state[key], (section, key))
                if section != "charge_state":
                    self.assertEqual(state["battery_level"], 80)
        for value in (None, [], True, "bad", 2):
            self.http.telemetry = telemetry()
            self.http.telemetry["vehicle_state"]["software_update"] = value
            _, state = await self.observe()
            for key in SCALARS:
                if key.startswith("software_update_"):
                    self.assertIsNone(state[key])
            self.assertTrue(state["locked"])
        self.http.telemetry = telemetry()
        self.http.telemetry["vehicle_state"].pop("software_update")
        for key in WHEELS:
            self.http.telemetry["vehicle_state"].pop(key)
        _, state = await self.observe()
        self.assertTrue(all(state[key] is None for key in WHEELS))
        self.assertIsNone(state["software_update_status"])

    async def test_boolean_text_validation_preserves_false_and_full_versions(self):
        for value in (True, False, None, 0, 1, "false", [], {}):
            self.http.telemetry["vehicle_state"]["locked"] = value
            self.http.telemetry["charge_state"]["charge_port_door_open"] = value
            _, state = await self.observe()
            expected = value if type(value) is bool else None
            self.assertIs(state["locked"], expected)
            self.assertIs(state["charge_port_door_open"], expected)
        for value in (None, "", " \t\n ", False, 3, [], {}, " full firmware hash ", "x" * 256):
            self.http.telemetry["vehicle_state"]["car_version"] = value
            self.http.telemetry["vehicle_state"]["software_update"].update(status=value, version=value)
            _, state = await self.observe()
            for key in TEXT:
                self.assertEqual(state[key], value.strip() or None if type(value) is str else None)

    async def test_manual_and_positive_delayed_auto_update_expanded_state_and_fresh_timestamp(self):
        clock = timestamp.ObservationClock
        clock.current = datetime.datetime(2026, 10, 3, tzinfo=datetime.timezone.utc)
        with mock.patch.object(tesla.datetime, "datetime", clock), self.fixture.clock.install():
            await self.fixture.handle("refresh", "")
            before = json.loads(self.fixture.fixture.retained[self.topic])
            reads = self.http.data_reads
            await self.fixture.handle("ac", "ON")
            event = await self.fixture.clock.next()
            self.assertEqual(self.http.data_reads, reads)
            self.http.telemetry["vehicle_state"]["locked"] = False
            self.http.telemetry["vehicle_state"]["tpms_pressure_fl"] = None
            clock.current += datetime.timedelta(minutes=3)
            job = self.fixture.session.jobs[self.fixture.fixture.identity]
            event.set()
            await asyncio.wait_for(job, 1)
            after = json.loads(self.fixture.fixture.retained[self.topic])
            self.assertEqual(self.http.data_reads, reads + 1)
            self.assertNotEqual(before["observed_at"], after["observed_at"])
            self.assertEqual(after["observed_at"], clock.current.isoformat())
            self.assertEqual(after["charge_finish_eta"], (clock.current + datetime.timedelta(minutes=12.5)).isoformat())
            self.assertFalse(after["locked"])
            self.assertIsNone(after["tpms_pressure_fl"])
            self.assertEqual(after["odometer_km"], before["odometer_km"])
            self.assertEqual(self.http.commands, 1)
        self.fixture.fixture.assert_worker_requests()

    async def test_chat_range_rate_unit_correction_keeps_current_limit_and_delta(self):
        context = control.CommandContext(False, self.fixture.fixture.chat)
        for distance, expected in (("km/hr", "19.7145 km/h"), ("mi/hr", "12.25 mi/h")):
            self.http.telemetry["gui_settings"]["gui_distance_units"] = distance
            await self.app._command_info(context, ((None, None), ()))
            message = self.fixture.fixture.chat.messages[-1][1]
            self.assertIn("Charge rate: " + expected, message)
            self.assertIn("Charge current limit: 16A", message)
            self.assertNotIn("Charge rate: 12.25A", message)
            await self.app._command_info(context, (("delta", None), ()))
            self.assertEqual(self.fixture.fixture.chat.messages[-1][1], "Nothing changed")

    async def test_failed_and_cancelled_expanded_reads_preserve_retained_state_and_no_new_routes(self):
        with self.fixture.clock.install():
            await self.fixture.handle("refresh", "")
            before = dict(self.fixture.fixture.retained)
            self.http.telemetry["vehicle_state"]["locked"] = False
            self.http.data_status = 400
            await self.fixture.handle("ac", "ON")
            event = await self.fixture.clock.next()
            job = self.fixture.session.jobs[self.fixture.fixture.identity]
            event.set()
            with self.assertLogs("teslabot.mqtt", "WARNING") as logs:
                await asyncio.wait_for(job, 1)
            self.assertIn("read failure detail", "\n".join(logs.output))
            self.assertEqual(self.fixture.fixture.retained, before)
            self.http.data_status = 200
            await self.fixture.handle("ac", "ON")
            await self.fixture.clock.next()
            reads = self.http.data_reads
            await self.mqtt._stop_session(self.fixture.session)
            self.assertEqual(self.http.data_reads, reads)
            self.assertEqual(self.fixture.fixture.retained, before)
        # Restore a live session to test admission, not merely inactive filtering.
        self.fixture.session.active = True
        calls = len(self.http.calls)
        publications = len(self.fixture.fixture.publications)
        for operation in ("seat_heater_left", "locked", "software_update", "door_driver_front_open", "version"):
            await self.fixture.handle(operation, "ON")
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(len(self.fixture.fixture.publications), publications)

    async def test_discovery_exact_readonly_entities_units_labels_and_namespace(self):
        identity = self.fixture.fixture.identity
        configs = self.mqtt._discovery(identity, "Synthetic car")
        self.assertEqual(set(configs), LEGACY_ENTITIES | {"sensor/" + key for key in SCALARS} | {"binary_sensor/" + key for key in BINARIES})
        for key, config in configs.items():
            self.assertEqual(config["availability_topic"], "teslabot/availability")
            self.assertEqual(config["device"]["identifiers"], ["teslabot_" + identity])
            self.assertNotIn("sw_version", config["device"])
            if key not in LEGACY_ENTITIES:
                self.assertNotIn("command_topic", config)
                self.assertEqual(config["state_topic"], self.topic)
                self.assertEqual(config["unique_id"], f"teslabot_{identity}_{key.split('/')[1]}")
                self.assertIn("'None'", config["value_template"])
        for key, device_class, unit, state_class in (
            ("inside_temp", "temperature", "\u00b0C", "measurement"),
            ("outside_temp", "temperature", "\u00b0C", "measurement"),
            ("charge_amps", "current", "A", "measurement"),
            ("charger_power_kw", "power", "kW", "measurement"),
            ("charge_rate_kmh", "speed", "km/h", "measurement"),
            ("odometer_km", "distance", "km", "total_increasing"),
            *((key, "pressure", "bar", "measurement") for key in WHEELS),
            ("charge_finish_eta", "timestamp", None, None),
            ("software_update_expected_duration_s", "duration", "s", None),
        ):
            config = configs["sensor/" + key]
            self.assertEqual((config.get("device_class"), config.get("unit_of_measurement"), config.get("state_class")),
                             (device_class, unit, state_class))
        for key in SEATS:
            self.assertNotIn("Driver", configs["sensor/" + key]["name"])
            self.assertNotIn("Passenger", configs["sensor/" + key]["name"])
            self.assertNotIn("state_class", configs["sensor/" + key])
        self.assertEqual(configs["sensor/seat_heater_left"]["name"], "Front left seat heat level")
        self.assertEqual(configs["sensor/seat_heater_rear_center"]["name"], "Rear center seat heat level")
        for key in BINARIES:
            device_class = "lock" if key == "locked" else "door" if key.startswith("door_") else "window" if key.startswith("window_") else "opening"
            self.assertEqual(configs["binary_sensor/" + key]["device_class"], device_class)
        for key in TEXT + ("software_update_download_percent", "software_update_install_percent", "software_update_expected_duration_s"):
            self.assertEqual(configs["sensor/" + key]["entity_category"], "diagnostic")
        for key in ("software_update_download_percent", "software_update_install_percent"):
            self.assertEqual(configs["sensor/" + key]["unit_of_measurement"], "%")
            self.assertNotIn("device_class", configs["sensor/" + key])
            self.assertNotIn("state_class", configs["sensor/" + key])
        self.mqtt.prefix = "other/namespace"
        self.mqtt._entity_prefix = "teslabot_other"
        other = self.mqtt._discovery(identity, "Synthetic car")
        for key, config in other.items():
            self.assertNotEqual(config["unique_id"], configs[key]["unique_id"])
            self.assertEqual(config["device"]["identifiers"], ["teslabot_other_" + identity])
            if "state_topic" in config:
                self.assertEqual(config["state_topic"], f"other/namespace/{identity}/state")

    async def test_optional_templates_reset_known_zero_false_unknown_and_older_payloads(self):
        if jinja2 is None:
            self.skipTest("Jinja2 is needed only for actual HA template rendering")
        configs = self.mqtt._discovery(self.fixture.fixture.identity, "Synthetic car")
        env = jinja2.Environment()
        _, state = await self.observe()
        for key in SCALARS:
            template = env.from_string(configs["sensor/" + key]["value_template"])
            for value in (state[key], 0 if key not in TEXT + ("charge_finish_eta",) else "synthetic text", None, state[key]):
                rendered = template.render(value_json={key: value})
                self.assertEqual(rendered, str(value) if value is not None else "None", key)
            self.assertEqual(template.render(value_json={}), "None")
            if key in TEXT:
                self.assertEqual(template.render(value_json={key: "x" * 255}), "x" * 255)
                self.assertEqual(template.render(value_json={key: "x" * 256}), "None")
        for key in BINARIES:
            template = env.from_string(configs["binary_sensor/" + key]["value_template"])
            for value in (True, False, None, True, 0, 1, "false"):
                expected = ("OFF" if value is True else "ON" if value is False else "None") if key == "locked" else (
                    "ON" if value is True else "OFF" if value is False else "None")
                self.assertEqual(template.render(value_json={key: value}), expected)
            self.assertEqual(template.render(value_json={}), "None")


class InstanceVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_vehicles_share_one_instance_version_outside_manifest(self):
        fixture = delay.DelayTests()
        fixture.setUp()
        try:
            app, mqtt, broker = fixture.app, fixture.mqtt, fixture.broker
            app._get_vehicle_list.return_value = [{"display_name": "One"}, {"display_name": "Two"}]
            app._vehicle_id.side_effect = lambda vehicle: fixture.id1 if vehicle["display_name"] == "One" else fixture.id2
            await mqtt._reconcile(broker, 0)
            versions = [(topic, payload) for topic, payload, _ in broker.publications
                        if topic.endswith("/version/config")]
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0][0], "homeassistant/sensor/teslabot/version/config")
            self.assertEqual(json.loads(fixture.state["mqtt_owned"][mqtt._manifest_key]), sorted([fixture.id1, fixture.id2]))
            for identity in (fixture.id1, fixture.id2):
                self.assertNotIn("sensor/version", mqtt._discovery(identity, "Synthetic car"))
            app.refresh_vehicle.assert_not_awaited()
        finally:
            await fixture.asyncTearDown()

    async def test_global_version_without_observation_auth_or_vehicles_and_owned_cleanup(self):
        fixture = delay.DelayTests()
        fixture.setUp()
        mqtt, app, broker = fixture.mqtt, fixture.app, fixture.broker
        try:
            old = fixture.id1
            fixture.state["mqtt_owned"] = {mqtt._manifest_key: json.dumps([old])}
            for authorized in (True, False):
                app.authorized = authorized
                app._get_vehicle_list.return_value = []
                broker.publications.clear()
                broker.subscriptions.clear()
                self.assertTrue(await mqtt._reconcile(broker, 0))
                config_topic = "homeassistant/sensor/teslabot/version/config"
                config = json.loads(broker.retained[config_topic])
                self.assertEqual(config["state_topic"], "teslabot/version")
                self.assertEqual(config["unique_id"], "teslabot_version")
                self.assertEqual(config["device"], mqtt._delay_discovery()["device"])
                self.assertEqual(config["device"]["sw_version"], __version__)
                self.assertEqual(config["entity_category"], "diagnostic")
                self.assertNotIn("command_topic", config)
                self.assertEqual(broker.retained["teslabot/version"], __version__)
                self.assertEqual(sum(topic == config_topic for topic, _, _ in broker.publications), 1)
                self.assertEqual(json.loads(fixture.state["mqtt_owned"][mqtt._manifest_key]), [])
                self.assertEqual(broker.retained["teslabot/availability"], "online" if authorized else "offline")
                self.assertFalse(any(topic.endswith("/state") and old in topic and payload for topic, payload, _ in broker.publications))
                app.refresh_vehicle.assert_not_awaited()
                for topic, _, kwargs in broker.publications:
                    self.assertEqual(kwargs, {"qos": 1, "retain": True})
                if authorized:
                    deleted = {topic for topic, payload, _ in broker.publications if payload == ""}
                    self.assertEqual(deleted, {mqtt._config_topic(old, key) for key in mqtt._discovery(old, "")} | {f"teslabot/{old}/state"})
                    self.assertLess(next(i for i, entry in enumerate(broker.publications) if entry[0] == "teslabot/version"),
                                    next(i for i, entry in enumerate(broker.publications) if entry[1] == "online"))
            other = fixture.build({"prefix": "other/instance", "discovery_prefix": "custom/discovery"})
            other.set_app(app)
            await other._reconcile(broker, 0)
            other_config = other._version_discovery()
            self.assertNotEqual(other_config["unique_id"], config["unique_id"])
            self.assertNotEqual(other_config["device"]["identifiers"], config["device"]["identifiers"])
            self.assertEqual(other_config["state_topic"], "other/instance/version")
            self.assertIn(f"custom/discovery/sensor/{other._entity_prefix}/version/config", broker.retained)
            self.assertEqual(broker.retained["other/instance/version"], __version__)
            await other.close()
        finally:
            await fixture.asyncTearDown()
