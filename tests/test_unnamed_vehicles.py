import asyncio
import dataclasses
import threading
import unittest
from unittest import mock

import teslapy

from teslabot import control, tesla
import tests.test_sdk_boundary as boundary


class UnnamedVehicleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self):
        self.fixture = boundary.SDKBoundaryTests()
        self.fixture.setUp()
        self.app, self.http, self.chat = self.fixture.app, self.fixture.http, self.fixture.chat
        self.identity = self.fixture.identity

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def name(self, variant):
        for data in (self.http.product, self.http.telemetry):
            if variant == "absent":
                data.pop("display_name", None)
            else:
                data["display_name"] = None if variant == "null" else ""

    def assert_no_alias(self):
        before = len(self.http.calls)
        validator = tesla.ValidVehicle(self.app).make_validator()
        self.assertEqual(validator.parse(["Unnamed vehicle"]).__class__.__name__, "ParseFail")
        for metadata in self.app.cached_vehicle_list:
            self.assertIs(type(metadata), dict)
            self.assertFalse(metadata.get("display_name"))
            self.assertEqual(self.app._vehicle_id(metadata), self.identity)
        self.assertEqual(len(self.http.calls), before)

    def assert_name(self, vehicle, variant):
        if variant == "absent":
            self.assertNotIn("display_name", vehicle)
        else:
            self.assertIn("display_name", vehicle)
            self.assertEqual(vehicle.get("display_name"), None if variant == "null" else "")

    async def test_online_blank_product_refreshes_and_all_mqtt_actions_publish(self):
        self.name("blank")
        for operation, payload in (("refresh", ""), ("ac", "ON"), ("sauna", "OFF"), ("charge_limit", "70")):
            with self.subTest(operation=operation):
                self.fixture.publications.clear()
                reads = self.http.data_reads
                await self.fixture.handle(operation, payload)
                self.assertEqual(self.http.data_reads, reads + 1)
                self.assertEqual(len(self.fixture.publications), 1 if operation == "refresh" else 2)
                if operation != "refresh":
                    self.assertTrue(self.fixture.publications[0][1]["success"])
                    self.assertEqual(self.fixture.publications[0][1]["vehicle_id"], self.identity)
                topic, state, kwargs = self.fixture.publications[-1]
                self.assertEqual(topic, f"teslabot/{self.identity}/state")
                self.assertEqual(state["vehicle_id"], self.identity)
                self.assertEqual(state["display_name"], "Unnamed vehicle")
                self.assertEqual(state["battery_level"], 80)
                self.assertTrue(kwargs["retain"])
                self.assert_name(self.fixture.vehicles[-1], "blank")
                self.assert_no_alias()
        self.assertFalse(any(call[0].endswith("/wake_up") for call in self.http.calls))
        self.fixture.assert_worker_requests()

    async def test_null_and_absent_names_work_for_mqtt_and_single_vehicle_paths(self):
        for variant in ("null", "absent"):
            with self.subTest(variant=variant):
                self.name(variant)
                reads = self.http.data_reads
                snapshot = await self.app.refresh_vehicle(None)
                self.assertEqual(snapshot.vehicle_id, self.identity)
                self.assertEqual(snapshot.display_name, "Unnamed vehicle")
                self.assert_name(snapshot.data, variant)
                before = dataclasses.asdict(snapshot)
                for method, value in self.fixture.actions():
                    result = await method(None, value)
                    self.assertTrue(result.success)
                    self.assertEqual(result.vehicle_id, self.identity)
                    self.assert_name(self.fixture.vehicles[-1], variant)
                self.assertEqual(self.http.data_reads, reads + 1)
                for operation, payload in (("refresh", ""), ("ac", "ON"), ("sauna", "OFF"), ("charge_limit", "70")):
                    self.fixture.publications.clear()
                    await self.fixture.handle(operation, payload)
                    self.assertTrue(self.fixture.publications[-1][0].endswith("/state"))
                await self.app._command_on_vehicle(control.CommandContext(False, self.chat), None,
                                                   lambda vehicle: vehicle.command("LOCK"))
                self.assertEqual(self.chat.messages[-1][1], "Success!")
                self.assertEqual(before, dataclasses.asdict(snapshot))
                self.assert_no_alias()
        self.fixture.assert_worker_requests()

    async def test_asleep_unnamed_wake_and_summary_do_not_lazy_fetch_telemetry(self):
        original = teslapy.Vehicle.sync_wake_up
        observed = []
        def wake(vehicle):
            observed.append((threading.get_ident(), self.app._operation_lock.locked(), vehicle.get("display_name")))
            return original(vehicle)
        with mock.patch("teslapy.Vehicle.sync_wake_up", new=wake), mock.patch("teslapy.time.sleep") as sleep, \
             self.assertLogs("teslapy", "INFO") as logs:
            for variant in ("blank", "null", "absent"):
                self.name(variant)
                self.http.product["state"] = "asleep"
                reads = self.http.data_reads
                result = await self.app.set_ac(None, True)
                self.assertTrue(result.success)
                self.assertEqual(result.vehicle_id, self.identity)
                self.assertEqual(self.http.data_reads, reads)
                self.assert_name(self.fixture.vehicles[-1], variant)
                self.assertEqual(self.fixture.vehicles[-1].get("state"), "online")
                self.assert_no_alias()
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(sum(call[0].endswith("/wake_up") for call in self.http.calls), 3)
        self.assertEqual(sum(call[0].endswith("/vehicles/1") for call in self.http.calls), 3)
        self.assertTrue(all(thread != self.fixture.main_thread and locked and name == "Unnamed vehicle"
                            for thread, locked, name in observed))
        self.assertIn("Unnamed vehicle is asleep", "\n".join(logs.output))
        self.assertIn("Unnamed vehicle is online", "\n".join(logs.output))
        self.fixture.assert_worker_requests()

    async def test_temporary_name_restored_after_wake_errors(self):
        for variant in ("blank", "null", "absent"):
            self.name(variant)
            with self.subTest(variant=variant), \
                 mock.patch("teslapy.Vehicle.sync_wake_up", side_effect=teslapy.VehicleError("wake diagnostic detail")):
                result = await self.app.set_sauna(None, False)
                self.assertFalse(result.success)
                self.assertEqual(result.vehicle_id, self.identity)
                self.assertIn("wake diagnostic detail", result.error or "")
                self.assert_name(self.fixture.vehicles[-1], variant)
                self.assert_no_alias()
        self.assertEqual(self.http.data_reads, 0)
        self.assertEqual(self.http.commands, 0)
        self.fixture.assert_worker_requests()

    async def test_real_summary_name_is_not_overwritten_by_logging_restore(self):
        for name in ("Observed summary name", "Unnamed vehicle"):
            self.name("absent")
            self.http.product["state"] = "asleep"
            self.http.summary["display_name"] = name
            with self.subTest(name=name), mock.patch("teslapy.time.sleep"):
                result = await self.app.set_ac(None, True)
            self.assertTrue(result.success)
            self.assertEqual(self.fixture.vehicles[-1].get("display_name"), name)
        self.assertEqual(self.http.data_reads, 0)
        self.assert_no_alias()  # enumeration cache still represents product metadata
        self.fixture.assert_worker_requests()

    async def test_cancellation_restores_name_before_releasing_operation_gate(self):
        self.name("absent")
        entered, release = threading.Event(), threading.Event()
        def wake(vehicle):
            self.assertEqual(vehicle.get("display_name"), "Unnamed vehicle")
            entered.set()
            release.wait(2)
        with mock.patch("teslapy.Vehicle.sync_wake_up", new=wake):
            caller = asyncio.create_task(self.app.set_ac(None, True))
            try:
                while not entered.is_set():
                    await asyncio.sleep(0)
                self.assert_no_alias()
                caller.cancel()
                await asyncio.sleep(0)
                self.assertFalse(caller.done())
                self.assertTrue(self.app._operation_lock.locked())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await caller
        self.assert_name(self.fixture.vehicles[-1], "absent")
        self.assertFalse(self.app._operation_lock.locked())
        self.assertEqual(self.http.data_reads, 0)
        self.assertEqual(self.http.commands, 0)
        self.fixture.assert_worker_requests()

    async def test_required_state_id_and_real_identity_still_fail_without_fetch(self):
        self.name("blank")
        async with self.app._operation():
            for field in ("state", "id_s"):
                for value in (None, "", 1):
                    vehicle = (await self.app._get_vehicle_list(sdk_objects=True))[0]
                    vehicle[field] = value
                    calls = len(self.http.calls)
                    with self.assertRaisesRegex(tesla.VehicleException, f"metadata missing {field}"):
                        await self.app._wake(None, vehicle)
                    self.assertEqual(len(self.http.calls), calls)
                vehicle = (await self.app._get_vehicle_list(sdk_objects=True))[0]
                vehicle.pop(field)
                with self.assertRaisesRegex(tesla.VehicleException, f"metadata missing {field}"):
                    await self.app._wake(None, vehicle)
            vehicle = (await self.app._get_vehicle_list(sdk_objects=True))[0]
            vehicle.pop("vin")
            with self.assertRaisesRegex(tesla.VehicleException, "no usable VIN or display name"):
                await self.app._wake(None, vehicle)
        self.assertEqual(self.http.data_reads, 0)
        self.fixture.assert_worker_requests()
