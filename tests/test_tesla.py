import asyncio
import unittest
from unittest import mock
from typing import Any, Dict, List, Tuple

from teslabot.config import Config
from teslabot.control import CommandContext, Control, MessageContext
from teslabot.env import Env
from teslabot.filestate import FileState
from teslabot import tesla


class FakeControl(Control):
    def __init__(self) -> None:
        super().__init__()
        self.messages: List[Tuple[MessageContext, str]] = []

    async def setup(self) -> None:
        pass

    async def send_message(self, message_context: MessageContext, message: str) -> None:
        self.messages.append((message_context, message))

    async def run(self) -> None:
        pass


class FakeTesla:
    def __init__(self, email: str, **kwargs: object) -> None:
        self.email = email
        self.kwargs: Dict[str, object] = kwargs
        self.authorized = False
        self.fetch_token_calls: List[str] = []
        self.logout_calls = 0

    def authorization_url(self) -> str:
        return "https://auth.tesla.com/oauth2/v3/authorize"

    def fetch_token(self, *, authorization_response: str) -> None:
        self.fetch_token_calls.append(authorization_response)
        self.authorized = True

    def logout(self) -> None:
        self.logout_calls += 1
        self.authorized = False

    def vehicle_list(self):
        return []

    def close(self):
        pass


class TestTeslaAuthorization(unittest.TestCase):
    def setUp(self) -> None:
        self.tesla_class = mock.patch("teslabot.tesla.TeslaSession", FakeTesla)
        self.tesla_class.start()
        self.addCleanup(self.tesla_class.stop)
        config = Config("test.ini", {
            "common": {"storage": "local"},
            "tesla": {
                "email": "driver@example.com",
                "credentials_store": "test-cache.json",
                "override_vehicles": "all",
            },
        })
        self.control = FakeControl()
        self.app = tesla.App(self.control, Env(config, FileState("test-state.ini")))
        self.admin_context = CommandContext(admin_room=True, control=self.control, txn="test")
        self.non_admin_context = CommandContext(admin_room=False, control=self.control, txn="test")

    def test_authorize_requires_admin_room_and_returns_url(self) -> None:
        asyncio.get_event_loop().run_until_complete(self.app._command_authorized(self.non_admin_context, None))

        self.assertEqual(self.control.messages[-1][1], "Please use the admin room for this command.")

        asyncio.get_event_loop().run_until_complete(self.app._command_authorized(self.admin_context, None))

        self.assertIn("https://auth.tesla.com/oauth2/v3/authorize", self.control.messages[-1][1])
        self.assertEqual(self.app.tesla.email, "driver@example.com")
        self.assertEqual(self.app.tesla.kwargs["cache_file"], "test-cache.json")

    def test_authorize_exchanges_callback_url_and_reports_success(self) -> None:
        callback_url = "https://auth.tesla.com/void/callback?code=secret"

        asyncio.get_event_loop().run_until_complete(self.app._command_authorized(self.admin_context, callback_url))

        self.assertEqual(self.app.tesla.fetch_token_calls, [callback_url])
        self.assertEqual(self.control.messages[-1][1], "Authorization successful")

    def test_logout_preserves_admin_only_behavior(self) -> None:
        self.app.tesla.authorized = True

        asyncio.get_event_loop().run_until_complete(self.app._command_logout(self.non_admin_context, ()))
        self.assertEqual(self.control.messages[-1][1], "Please use the admin room for this command.")
        self.assertEqual(self.app.tesla.logout_calls, 0)

        asyncio.get_event_loop().run_until_complete(self.app._command_logout(self.admin_context, ()))
        self.assertEqual(self.app.tesla.logout_calls, 1)
        self.assertEqual(self.control.messages[-1][1], "Logout successful!")

    def test_typed_actions_use_existing_tesla_commands(self) -> None:
        vehicle = {"display_name": "Test vehicle", "vin": "VIN1"}
        with mock.patch.object(self.app, "_execute_vehicle", new=mock.AsyncMock(return_value=(vehicle, True))) as execute:
            result = asyncio.get_event_loop().run_until_complete(self.app.set_ac(None, True))
            self.assertTrue(result.success)
            car = mock.Mock()
            execute.call_args.args[1](car)
            car.command.assert_called_once_with("CLIMATE_ON")
            result = asyncio.get_event_loop().run_until_complete(self.app.set_sauna(None, False))
            self.assertTrue(result.success)
            car.command.reset_mock()
            execute.call_args.args[1](car)
            car.command.assert_called_once_with("MAX_DEFROST", on=False)
            result = asyncio.get_event_loop().run_until_complete(self.app.set_charge_limit(None, 70))
            self.assertTrue(result.success)
            car.command.reset_mock()
            execute.call_args.args[1](car)
            car.command.assert_called_once_with("CHANGE_CHARGE_LIMIT", percent="70")
            with self.assertRaises(tesla.ArgException):
                asyncio.get_event_loop().run_until_complete(self.app.set_charge_limit(None, 101))

    def test_failed_legacy_vehicle_operation_does_not_report_success(self) -> None:
        with mock.patch.object(self.app, "_execute_vehicle", new=mock.AsyncMock(side_effect=tesla.ArgException("missing"))):
            asyncio.get_event_loop().run_until_complete(
                self.app._command_on_vehicle(self.admin_context, None, lambda vehicle: True))
        self.assertEqual(self.control.messages[-1][1], "Error: missing")

    def test_vehicle_identity_survives_rename_and_name_reuse(self) -> None:
        first = {"display_name": "Alpha", "vin": "VIN_A"}
        second = {"display_name": "Beta", "vin": "VIN_B"}
        first_id = self.app._vehicle_id(first)
        with mock.patch.object(self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=[first, second])):
            self.assertIs(asyncio.get_event_loop().run_until_complete(
                self.app._get_vehicle_by_id(first_id)), first)
        first["display_name"], second["display_name"] = "Beta", "Alpha"
        with mock.patch.object(self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=[first, second])):
            self.assertIs(asyncio.get_event_loop().run_until_complete(
                self.app._get_vehicle_by_id(first_id)), first)
        with mock.patch.object(self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=[second])):
            with self.assertRaises(tesla.ArgException):
                asyncio.get_event_loop().run_until_complete(self.app._get_vehicle_by_id(first_id))

    def test_duplicate_names_resolve_by_vehicle_id(self) -> None:
        vehicles = [{"display_name": "Same", "vin": vin} for vin in ("VIN_A", "VIN_B")]
        with mock.patch.object(self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=vehicles)):
            self.assertIs(asyncio.get_event_loop().run_until_complete(
                self.app._get_vehicle_by_id(self.app._vehicle_id(vehicles[1]))), vehicles[1])

    def test_action_failure_preserves_mqtt_identity(self) -> None:
        with mock.patch.object(self.app, "_execute_vehicle", new=mock.AsyncMock(side_effect=tesla.ArgException("missing"))):
            result = asyncio.get_event_loop().run_until_complete(
                self.app.set_ac(None, True, vehicle_id="id1"))
        self.assertFalse(result.success)
        self.assertEqual(result.vehicle_id, "id1")

    def test_snapshot_temperatures_are_celsius_even_if_gui_uses_fahrenheit(self) -> None:
        data = {"gui_settings": {"gui_temperature_units": "F"},
                "climate_state": {"inside_temp": 20.0, "outside_temp": -2.0},
                "charge_state": {}}
        vehicle = {"display_name": "Test vehicle", "vin": "VIN1"}
        with mock.patch.object(self.app, "_execute_vehicle", new=mock.AsyncMock(return_value=(vehicle, data))):
            snapshot = asyncio.get_event_loop().run_until_complete(self.app.refresh_vehicle(None))
        self.assertEqual((snapshot.inside_temp, snapshot.outside_temp, snapshot.temperature_unit),
                         (20.0, -2.0, "C"))

    def test_mqtt_run_does_not_start_persisted_scheduler(self) -> None:
        self.control.run_scheduled_commands = False
        self.app.tesla.authorized = True
        with mock.patch.object(self.app._scheduler, "start", new=mock.AsyncMock()) as start, \
             mock.patch.object(self.app, "_load_state", new=mock.AsyncMock()), \
             mock.patch.object(self.app, "_get_vehicle_list", new=mock.AsyncMock()):
            asyncio.get_event_loop().run_until_complete(self.app.initialize())
            start.assert_not_awaited()

    def test_info_shows_climate_state_and_target_temperatures(self) -> None:
        data: Dict[str, Any] = {
            "gui_settings": {"gui_distance_units": "km/hr", "gui_temperature_units": "C"},
            "drive_state": {},
            "charge_state": {
                "battery_level": 80,
                "battery_range": 300,
                "est_battery_range": 280,
                "charge_limit_soc": 90,
                "charge_current_request": 16,
                "scheduled_charging_mode": "Off",
                "scheduled_charging_start_time": None,
                "charge_rate": 0,
                "charging_state": "Disconnected",
                "time_to_full_charge": 0,
            },
            "vehicle_state": {
                "car_version": "test",
                "ft": 0,
                "rt": 0,
                "locked": True,
                "fd_window": 0,
                "fp_window": 0,
                "rd_window": 0,
                "rp_window": 0,
                "valet_mode": False,
                "odometer": 0,
                "vehicle_name": "Test vehicle",
            },
            "climate_state": {
                "inside_temp": 19.5,
                "outside_temp": 12,
                "is_climate_on": True,
                "is_preconditioning": False,
                "climate_keeper_mode": "off",
                "driver_temp_setting": 20,
                "passenger_temp_setting": 21,
                "seat_heater_left": 0,
                "seat_heater_right": 0,
                "seat_heater_rear_center": 0,
                "seat_heater_rear_left": 0,
                "seat_heater_rear_right": 0,
            },
        }
        with mock.patch.object(
            self.app, "_execute_vehicle", new=mock.AsyncMock(return_value=({"display_name": "Test vehicle", "vin": "VIN1"}, data))
        ), mock.patch.object(
            self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=[])
        ):
            asyncio.get_event_loop().run_until_complete(
                self.app._command_info(self.admin_context, ((None, None), ()))
            )

        self.assertIn("Climate: on Target: 20°C / 21°C", self.control.messages[-1][1])

        data["gui_settings"]["gui_temperature_units"] = "F"
        with mock.patch.object(
            self.app, "_execute_vehicle", new=mock.AsyncMock(return_value=({"display_name": "Test vehicle", "vin": "VIN1"}, data))
        ), mock.patch.object(
            self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=[])
        ):
            asyncio.get_event_loop().run_until_complete(
                self.app._command_info(self.admin_context, ((None, None), ()))
            )

        message = self.control.messages[-1][1]
        self.assertIn("Inside: 67.1°F Outside: 53.6°F", message)
        self.assertIn("Climate: on Target: 68°F / 69.8°F", message)

        data["climate_state"]["passenger_temp_setting"] = 20
        with mock.patch.object(
            self.app, "_execute_vehicle", new=mock.AsyncMock(return_value=({"display_name": "Test vehicle", "vin": "VIN1"}, data))
        ), mock.patch.object(
            self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=[])
        ):
            asyncio.get_event_loop().run_until_complete(
                self.app._command_info(self.admin_context, ((None, None), ()))
            )

        self.assertIn("Climate: on Target: 68°F", self.control.messages[-1][1])
        self.assertNotIn("68°F /", self.control.messages[-1][1])

        # A different adapter/room has not received the other destination's data.
        other = FakeControl()
        contexts = [CommandContext(True, other), CommandContext(False, self.control),
                    CommandContext(False, self.control, scheduled=True)]
        with mock.patch.object(self.app, "_execute_vehicle", new=mock.AsyncMock(return_value=({"display_name": "Test vehicle", "vin": "VIN1"}, data))), \
             mock.patch.object(self.app, "_get_vehicle_list", new=mock.AsyncMock(return_value=[])):
            for context in contexts:
                asyncio.get_event_loop().run_until_complete(self.app._command_info(context, (("delta", None), ())))
                self.assertIn("Climate: on", self.control.messages[-1][1])
            before = dict(self.app._prev_info)
            with mock.patch.object(self.control, "send_message", new=mock.AsyncMock(side_effect=RuntimeError("offline"))):
                with self.assertRaises(RuntimeError):
                    asyncio.get_event_loop().run_until_complete(self.app._command_info(CommandContext(False, other), (("delta", None), ())))
            self.assertEqual(before, self.app._prev_info)
