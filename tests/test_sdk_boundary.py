import asyncio
import dataclasses
import json
import threading
import unittest
import urllib.parse
from unittest import mock
from typing import Any, Awaitable, Callable, Optional

import oauthlib.oauth2
import requests
import requests.adapters
import teslapy
import urllib3.exceptions

from teslabot import control, tesla
from teslabot.mqtt import MqttControl
import tests.test_multi_control_review as fixtures


def telemetry():
    return {
        "display_name": "SDK car",
        "gui_settings": {"gui_distance_units": "km/hr", "gui_temperature_units": "C"},
        "drive_state": {"latitude": 60.0, "longitude": 24.0, "speed": 0, "heading": 90},
        "charge_state": {"battery_level": 80, "battery_range": 300, "est_battery_range": 280,
                         "charge_limit_soc": 90, "charge_current_request": 16,
                         "scheduled_charging_mode": "Off", "scheduled_charging_start_time": None,
                         "charge_rate": 0, "charging_state": "Disconnected", "time_to_full_charge": 0},
        "vehicle_state": {"car_version": "sdk", "ft": 0, "rt": 0, "locked": True,
                          "fd_window": 0, "fp_window": 0, "rd_window": 0, "rp_window": 0,
                          "valet_mode": False, "odometer": 10, "vehicle_name": "SDK car"},
        "climate_state": {"inside_temp": 20, "outside_temp": 10, "is_climate_on": True},
        "diagnostics": {"arbitrary": [{"detail": "preserved diagnostic payload"}]},
    }


class HttpAdapter(requests.adapters.BaseAdapter):
    def __init__(self, app):
        self.app = app
        self.calls = []
        self.data_reads = 0
        self.commands = 0
        self.product = {"id": 1, "id_s": "1", "vehicle_id": 1, "vin": "SDKVIN",
                        "display_name": "SDK car", "state": "online"}
        self.telemetry = telemetry()
        self.data_status = 200
        self.command_status = 200
        self.command_result = True
        self.command_error: Optional[BaseException] = None
        self.command_interrupted = False

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        path = urllib.parse.urlparse(request.url or "").path
        self.calls.append((path, threading.get_ident(), self.app._operation_lock.locked(),
                           self.app._operation_owner is not None, request.body))
        status = 200
        if path.endswith("/products"):
            payload = {"response": [self.product]}
        elif path.endswith("/vehicle_data"):
            self.data_reads += 1
            status = self.data_status
            payload = {"response": self.telemetry} if status == 200 else {"error": "read failure detail"}
        elif "/command/" in path:
            self.commands += 1
            if self.command_error is not None:
                raise self.command_error
            status = self.command_status
            payload = ({"response": {"result": self.command_result, "reason": "command rejection detail"}}
                       if status == 200 else {"error": "HTTP command failure detail"})
        else:
            payload = {"response": []}
        response = requests.Response()
        response.status_code = status
        response.url = request.url or ""
        response.request = request
        if self.command_interrupted and "/command/" in path:
            class Raw:
                def stream(self, chunk_size, decode_content=True):
                    raise urllib3.exceptions.ProtocolError("real response interruption detail")
                    yield b""
            response.raw = Raw()
        else:
            response._content = json.dumps(payload).encode()
        return response

    def close(self):
        pass


class SDKBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        fixture = fixtures.ReviewRegressionTests()
        fixture.setUp()
        self.app, self.chat, self.env = fixture.app, fixture.chat, fixture.env
        self.sdk = tesla.TeslaSession("sdk@example.com", timeout=30,
                                     cache_loader=lambda: {}, cache_dumper=lambda value: None)
        self.sdk.token = {"access_token": "test-token", "token_type": "Bearer"}
        self.app.tesla = self.sdk
        self.app.authorized = True
        self.http = HttpAdapter(self.app)
        self.sdk.mount("https://", self.http)
        self.vehicles = []
        original_list = self.sdk.vehicle_list
        def enumerate_vehicles():
            vehicles = original_list()
            self.vehicles.extend(vehicles)
            return vehicles
        self.sdk.vehicle_list = enumerate_vehicles
        self.mqtt = MqttControl(self.env)
        self.mqtt.set_app(self.app)
        self.identity = self.app._vehicle_id(self.http.product)
        self.mqtt.vehicles = {self.identity: "SDK car"}
        self.mqtt._generation = self.app.auth_generation
        self.main_thread = threading.get_ident()
        self.retained = {}
        self.publications = []
        async def publish(topic, payload, **kwargs):
            self.publications.append((topic, json.loads(payload), kwargs))
            if kwargs.get("retain"):
                self.retained[topic] = payload
        self.client = mock.Mock(publish=mock.AsyncMock(side_effect=publish))
        original_sleep = asyncio.sleep
        async def immediate(delay):
            await original_sleep(0)
        self.retry_sleep = mock.patch("teslabot.tesla.asyncio.sleep", side_effect=immediate)
        self.retry_sleep.start()

    async def asyncTearDown(self):
        self.retry_sleep.stop()
        await self.mqtt.close()
        await self.app.close()

    def assert_worker_requests(self):
        self.assertTrue(self.http.calls)
        for path, thread, locked, owned, _ in self.http.calls:
            self.assertNotEqual(thread, self.main_thread, path)
            self.assertTrue(locked, path)
            self.assertTrue(owned, path)

    async def handle(self, operation, payload):
        await self.mqtt._handle(self.client, f"teslabot/{self.identity}/{operation}/set", payload, False)

    def actions(self) -> list[tuple[Callable[..., Awaitable[tesla.ActionResult]], Any]]:
        return [(self.app.set_ac, True), (self.app.set_sauna, False), (self.app.set_charge_limit, 70)]

    async def test_real_refresh_and_all_successful_action_publications(self):
        for operation, value, endpoint, body in (
            ("refresh", "", None, None),
            ("ac", "ON", "auto_conditioning_start", {}),
            ("sauna", "OFF", "set_preconditioning_max", {"on": False}),
            ("charge_limit", "70", "set_charge_limit", {"percent": "70"}),
        ):
            with self.subTest(operation=operation):
                self.publications.clear()
                await self.handle(operation, value)
                self.assertEqual(len(self.publications), 1 if operation == "refresh" else 2)
                if endpoint is not None:
                    command = [call for call in self.http.calls if "/command/" in call[0]][-1]
                    self.assertTrue(command[0].endswith(endpoint))
                    self.assertEqual(json.loads(command[4]), body)
                    self.assertTrue(self.publications[0][1]["success"])
                    self.assertTrue(self.publications[0][0].endswith("/result"))
                topic, state, kwargs = self.publications[-1]
                self.assertEqual(topic, f"teslabot/{self.identity}/state")
                self.assertEqual(state["battery_level"], 80)
                self.assertEqual(state["charge_limit"], 90)  # observed, not requested 70
                self.assertEqual(state["climate_on"], True)
                self.assertIsInstance(state["inside_temp"], (int, float))
                self.assertIsInstance(state["observed_at"], str)
                self.assertEqual(kwargs, {"qos": 1, "retain": True})
                self.assertNotIn("data", state)
                self.assertNotIn("diagnostics", state)
        self.assert_worker_requests()

    async def test_snapshot_and_nested_sdk_mappings_are_detached_inside_worker(self):
        original_data = teslapy.Vehicle.get_vehicle_data
        threads = []
        def capture(vehicle, *args, **kwargs):
            value = original_data(vehicle, *args, **kwargs)
            value["nested_sdk"] = teslapy.JsonDict({"values": [teslapy.JsonDict({"items": [1, 2]})]})
            return value
        original_plain = tesla.plain_data
        def observe(value):
            threads.append((threading.get_ident(), self.app._operation_lock.locked()))
            return original_plain(value)
        with mock.patch("teslapy.Vehicle.get_vehicle_data", new=capture), \
             mock.patch("teslabot.tesla.plain_data", side_effect=observe):
            snapshot = await self.app.refresh_vehicle(None)
        self.assertTrue(all(thread != self.main_thread and locked for thread, locked in threads))
        self.assertIs(type(snapshot.data), dict)
        self.assertIs(type(snapshot.data["nested_sdk"]), dict)
        self.assertIs(type(snapshot.data["nested_sdk"]["values"][0]), dict)
        before = dataclasses.asdict(snapshot)
        raw = self.vehicles[-1]
        self.assertIsInstance(raw, teslapy.Vehicle)
        async with self.app._operation():
            def mutate():
                raw["nested_sdk"]["values"][0]["items"].append(3)
                raw["charge_state"]["battery_level"] = 99
                raw["diagnostics"]["arbitrary"][0]["detail"] = "changed later"
            await self.app._retry_to_async(mutate)
            self.http.telemetry["charge_state"]["battery_level"] = 42
            await self.app._retry_to_async(raw.get_vehicle_data)
        await self.mqtt._publish_snapshot(self.client, snapshot)
        self.assertEqual(before, dataclasses.asdict(snapshot))
        self.assertEqual(snapshot.battery_level, 80)
        self.assertEqual(snapshot.data["charge_state"]["battery_level"], 80)
        self.assertEqual(snapshot.data["nested_sdk"]["values"][0]["items"], [1, 2])
        self.assertTrue(all(type(vehicle) is dict for vehicle in self.app.cached_vehicle_list))
        self.assert_worker_requests()

    async def test_mqtt_projection_never_traverses_raw_vehicle_data(self):
        snapshot = await self.app.refresh_vehicle(None)
        raw = self.vehicles[-1]
        snapshot.data = raw
        before = dict(raw)
        calls = len(self.http.calls)
        await self.mqtt._publish_snapshot(self.client, snapshot)
        self.assertIs(snapshot.data, raw)
        self.assertEqual(before, dict(raw))
        self.assertEqual(len(self.http.calls), calls)
        self.assertNotIn("data", self.publications[-1][1])

    async def test_partial_and_null_data_are_unknown_or_controlled_without_fetch(self):
        context = control.CommandContext(False, self.chat)
        for section in ("gui_settings", "charge_state", "vehicle_state"):
            for null in (False, True):
                with self.subTest(section=section, null=null):
                    self.http.telemetry = telemetry()
                    if null:
                        self.http.telemetry[section] = None
                    else:
                        self.http.telemetry.pop(section)
                    reads = self.http.data_reads
                    await self.app._command_info(context, ((None, None), ()))
                    self.assertEqual(self.http.data_reads, reads + 1)
                    self.assertIn("Vehicle information unavailable:", self.chat.messages[-1][1])
        self.http.telemetry = telemetry()
        self.http.telemetry["drive_state"] = None
        self.http.telemetry["climate_state"] = None
        reads = self.http.data_reads
        await self.app._command_info(context, ((None, None), ()))
        self.assertEqual(self.http.data_reads, reads + 1)
        self.assertIn("Location: unknown", self.chat.messages[-1][1])
        self.assertIn("Climate: unknown", self.chat.messages[-1][1])
        self.assertIn("Battery: 80%", self.chat.messages[-1][1])
        calls = len(self.http.calls)
        tesla.ValidVehicle(self.app).make_validator()
        self.assertEqual(len(self.http.calls), calls)
        self.assert_worker_requests()

    async def test_scalar_types_are_not_coerced_into_observations(self):
        self.http.telemetry = {"charge_state": {"battery_level": "80", "charging_state": 3,
                                               "charge_limit_soc": False, "charge_current_request": 16.5},
                               "climate_state": {"is_climate_on": "false", "defrost_mode": True,
                                                 "inside_temp": "20", "outside_temp": False}}
        snapshot = await self.app.refresh_vehicle(None)
        for key in ("battery_level", "charging_state", "charge_limit", "charge_amps", "climate_on",
                    "defrost_mode", "inside_temp", "outside_temp"):
            self.assertIsNone(getattr(snapshot, key), key)
        self.assertEqual(snapshot.data["charge_state"]["battery_level"], "80")
        await self.mqtt._publish_snapshot(self.client, snapshot)
        self.assertIsNone(self.publications[-1][1]["climate_on"])
        self.http.telemetry = telemetry()
        self.http.telemetry["vehicle_state"]["locked"] = None
        await self.app._command_info(control.CommandContext(False, self.chat), ((None, None), ()))
        self.assertIn("vehicle_state.locked", self.chat.messages[-1][1])
        self.assert_worker_requests()

    async def test_complete_info_wording_and_delta_remain_observation_based(self):
        context = control.CommandContext(False, self.chat)
        await self.app._command_info(context, ((None, None), ()))
        self.assertIn("SDK car version sdk", self.chat.messages[-1][1])
        self.assertIn("Battery: 80% 300 km est. 280 km", self.chat.messages[-1][1])
        self.assertIn("Inside: 20°C Outside: 10°C", self.chat.messages[-1][1])
        await self.app._command_info(context, (("delta", None), ()))
        self.assertEqual(self.chat.messages[-1][1], "Nothing changed")
        self.http.telemetry["charge_state"]["battery_level"] = 81
        await self.app._command_info(context, (("delta", None), ()))
        self.assertIn("Battery: 81%", self.chat.messages[-1][1])
        self.assertEqual(self.http.data_reads, 3)
        self.assert_worker_requests()

    async def test_metadata_missing_fields_do_not_trigger_lazy_fetch(self):
        self.http.product.pop("display_name")
        metadata = await self.app._get_vehicle_list()
        self.assertIs(type(metadata[0]), dict)
        self.assertEqual(tesla.vehicle_display_name(metadata[0]), "Unnamed vehicle")
        calls = len(self.http.calls)
        self.assertEqual(tesla.ValidVehicle(self.app).make_validator().parse(["SDK car"]).__class__.__name__, "ParseFail")
        self.assertEqual(self.app._vehicle_id(metadata[0]), self.identity)
        self.assertEqual(len(self.http.calls), calls)
        with self.assertRaisesRegex(tesla.VehicleException, "metadata missing display_name"):
            await self.app.refresh_vehicle(None)
        self.assertEqual(self.http.data_reads, 0)
        result = await self.app.set_ac(None, True)
        self.assertFalse(result.success)
        self.assertEqual(result.vehicle_id, self.identity)
        with self.assertRaises(tesla.VehicleException):
            self.app._vehicle_id({})
        self.assertEqual(self.http.data_reads, 0)
        self.assert_worker_requests()

    async def test_location_consumers_use_detached_data_and_report_unavailable(self):
        captured = []
        async def location_command(context):
            captured.append(await context.get_location(None))
        for drive in ({"latitude": 60.0, "longitude": 24.0}, None, {}, {"latitude": "60", "longitude": False}):
            self.http.telemetry = telemetry()
            self.http.telemetry["drive_state"] = drive
            reads = self.http.data_reads
            await self.app._command_location(control.CommandContext(False, self.chat), location_command)
            self.assertEqual(self.http.data_reads, reads + 1)
        self.assertEqual((captured[0].lat, captured[0].lon), (60.0, 24.0))
        self.assertEqual(captured[1:], [None, None, None])
        self.assertIn("location is unavailable", self.chat.messages[-1][1])
        self.assert_worker_requests()

    async def test_real_command_success_and_rejection_all_typed_apis(self):
        for method, value in self.actions():
            with self.subTest(method=method.__name__):
                self.http.command_result = True
                accepted = await method(None, value)
                self.assertIsInstance(accepted, tesla.ActionResult)
                self.assertTrue(accepted.success)
                self.assertEqual(accepted.vehicle_id, self.identity)
                self.assertEqual(accepted.requested_value, value)
                self.http.command_result = False
                rejected = await method(None, value)
                self.assertFalse(rejected.success)
                self.assertIn("command rejection detail", rejected.error or "")
                self.assertEqual(rejected.vehicle_id, self.identity)
                self.assertEqual(rejected.requested_value, value)
                json.dumps(dataclasses.asdict(rejected))
        self.assertEqual(self.http.data_reads, 0)
        self.assert_worker_requests()

    async def test_expected_real_requests_failures_return_typed_results(self):
        for status, error, expected_attempts, detail in (
            (400, None, 1, "HTTP command failure detail"),
            (503, None, 15, "HTTP command failure detail"),
            (200, requests.Timeout("timeout detail"), 15, "timeout detail"),
            (200, requests.ConnectionError("connection detail"), 15, "connection detail"),
            (200, urllib3.exceptions.ProtocolError("protocol detail"), 15, "protocol detail"),
            (200, requests.exceptions.ChunkedEncodingError("interrupted detail"), 15, "interrupted detail"),
        ):
            for method, value in self.actions():
                with self.subTest(status=status, error=type(error).__name__, method=method.__name__):
                    self.http.command_status, self.http.command_error = status, error
                    calls = self.http.commands
                    with self.assertLogs("teslabot.tesla", "ERROR") as logs:
                        result = await method(None, value)
                    self.assertFalse(result.success)
                    self.assertEqual(result.vehicle_id, self.identity)
                    self.assertEqual(result.requested_value, value)
                    self.assertIn(detail, result.error or "")
                    self.assertIn(detail, "\n".join(logs.output))
                    self.assertIn("Traceback", "\n".join(logs.output))
                    self.assertEqual(self.http.commands, calls + expected_attempts)
                    self.assertEqual(self.http.data_reads, 0)
        self.assert_worker_requests()

    async def test_auth_failure_returns_failure_and_invalidates_old_generation(self):
        self.http.command_status = 401
        result = await self.app.set_ac(None, True, vehicle_id=self.identity)
        self.assertFalse(result.success)
        self.assertEqual(result.vehicle_id, self.identity)
        self.assertIn("HTTP command failure detail", result.error or "")
        self.assertFalse(self.app.authorized)
        assert self.mqtt._generation is not None
        self.assertGreater(self.app.auth_generation, self.mqtt._generation)
        await self.handle("ac", "ON")
        self.assertEqual(self.publications, [])
        self.assertEqual(self.http.data_reads, 0)
        self.app._auth_changed(True)
        self.http.command_status = 200
        self.http.command_error = oauthlib.oauth2.InvalidGrantError(description="OAuth detail")
        result = await self.app.set_sauna(None, False)
        self.assertFalse(result.success)
        self.assertIn("OAuth detail", result.error or "")
        self.assertEqual(result.vehicle_id, self.identity)
        self.assertFalse(self.app.authorized)
        self.assert_worker_requests()

    async def test_failed_mqtt_actions_do_not_refresh_or_change_prior_state(self):
        await self.handle("refresh", "")
        prior = dict(self.retained)
        for mode in ("rejected", "http", "timeout"):
            self.http.command_result = mode != "rejected"
            self.http.command_status = 400 if mode == "http" else 200
            self.http.command_error = requests.Timeout("timeout detail") if mode == "timeout" else None
            for operation, payload in (("ac", "ON"), ("sauna", "OFF"), ("charge_limit", "70")):
                with self.subTest(mode=mode, operation=operation):
                    self.publications.clear()
                    reads = self.http.data_reads
                    await self.handle(operation, payload)
                    self.assertEqual(len(self.publications), 1)
                    self.assertTrue(self.publications[0][0].endswith("/result"))
                    self.assertFalse(self.publications[0][1]["success"])
                    self.assertEqual(self.http.data_reads, reads)
                    self.assertEqual(self.retained, prior)
        self.assert_worker_requests()

    async def test_followup_read_failure_preserves_truthful_acceptance_and_prior_state(self):
        await self.handle("refresh", "")
        prior = dict(self.retained)
        self.http.data_status = 400
        for operation, payload in (("ac", "ON"), ("sauna", "OFF"), ("charge_limit", "70")):
            with self.subTest(operation=operation):
                self.publications.clear()
                reads = self.http.data_reads
                with self.assertLogs("teslabot.mqtt", "WARNING") as logs:
                    await self.handle(operation, payload)
                self.assertEqual(len(self.publications), 1)
                self.assertTrue(self.publications[0][1]["success"])
                self.assertIn("read failure detail", "\n".join(logs.output))
                self.assertEqual(self.http.data_reads, reads + 1)
                self.assertEqual(self.retained, prior)
        self.assert_worker_requests()

    async def test_invalid_arguments_cancellation_and_programming_errors_propagate(self):
        invalid: list[tuple[Callable[..., Awaitable[tesla.ActionResult]], Any]] = [
            (self.app.set_ac, "ON"), (self.app.set_sauna, 1),
            (self.app.set_charge_limit, True), (self.app.set_charge_limit, 101)]
        for method, value in invalid:
            with self.assertRaises(tesla.ArgException):
                await method(None, value)
        self.assertEqual(self.http.calls, [])
        for error in (RuntimeError("programming detail"), requests.exceptions.InvalidURL("invalid URL detail"),
                      control.ConfigError("configuration detail"), asyncio.CancelledError()):
            self.http.command_error = error
            with self.assertRaises(type(error)):
                await self.app.set_ac(None, True)
        self.assertEqual(self.http.data_reads, 0)
        self.assert_worker_requests()

    async def test_actual_requests_interrupted_command_response_is_typed_failure(self):
        self.http.command_interrupted = True
        with self.assertLogs("teslabot.tesla", "ERROR") as logs:
            result = await self.app.set_ac(None, True)
        self.assertFalse(result.success)
        self.assertEqual(result.vehicle_id, self.identity)
        self.assertIn("real response interruption detail", result.error or "")
        self.assertIn("ChunkedEncodingError", "\n".join(logs.output))
        self.assertEqual(self.http.commands, 15)
        self.assertEqual(self.http.data_reads, 0)
        self.assert_worker_requests()

    async def test_sdk_object_selection_is_internal_to_operation_gate(self):
        with self.assertRaisesRegex(tesla.AppException, "operation gate"):
            await self.app._get_vehicle_list(sdk_objects=True)
        self.assertEqual(self.http.calls, [])
        async with self.app._operation():
            vehicles = await self.app._get_vehicle_list(sdk_objects=True)
            self.assertIsInstance(vehicles[0], teslapy.Vehicle)
        self.assertIs(type(self.app.cached_vehicle_list[0]), dict)
        self.assert_worker_requests()

    async def test_missing_null_and_nonmapping_sections_publish_unknown_scalars(self):
        for value in (None, [], "unavailable", 4):
            with self.subTest(value=value):
                self.http.telemetry = {"charge_state": value, "climate_state": value}
                self.publications.clear()
                await self.handle("refresh", "")
                self.assertEqual(len(self.publications), 1)
                state = self.publications[0][1]
                for field in ("battery_level", "charging_state", "charge_limit", "charge_amps", "climate_on",
                              "defrost_mode", "inside_temp", "outside_temp"):
                    self.assertIsNone(state[field], field)
        self.assert_worker_requests()
