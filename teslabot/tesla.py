import asyncio
from typing import List, Optional, Tuple, Callable, Awaitable, Any, TypeVar, Dict, Union, cast, NewType
import re
import datetime
from configparser import ConfigParser
from dataclasses import dataclass
import traceback
import json
from enum import Enum
import math
import time
from abc import ABC, abstractmethod

import teslapy
from urllib.error import HTTPError

from .control import Control, ControlCallback, CommandContext, MessageContext
from .commands import Invocation
from . import log
from .config import Config
from .state import State, StateElement
from . import commands
from . import parser as p
from .utils import assert_some, indent, call_with_delay_info, coalesce, round_to_next_second, map_optional
from .env import Env
from .locations import Location, Locations, LocationArgs, LocationArgsParser, LocationCommandContextBase, LocationInfoCoords, LatLon
from .asyncthread import to_async
from . import __version__
from .appscheduler import AppScheduler
from google.cloud import firestore # type: ignore

logger = log.getLogger(__name__)

T = TypeVar('T')

DEFAULT_NEAR_THRESHOLD_KM = 0.5

class AppException(Exception):
    pass

class ArgException(AppException):
    pass

class VehicleException(AppException):
    pass

VehicleName = NewType('VehicleName', str)

class ValidVehicle(p.Map[str, VehicleName]):
    tesla: teslapy.Tesla

    def __init__(self, tesla: teslapy.Tesla) -> None:
        super().__init__(map=lambda x: VehicleName(x),
                         parser=p.Delayed[str](self.make_validator))
        self.tesla = tesla

    def make_validator(self) -> p.Parser[str]:
        vehicles = self.tesla.vehicle_list()
        display_names = [vehicle["display_name"] for vehicle in vehicles]
        return p.OneOfStrings(display_names)

class LocationDetail(Enum):
    Full = "full"       # show precise location information
    Near = "near"       # show precise location is near some predefined location
    At = "at"           # show only if location is near some predefined location
    Nearest = "nearest" # show distance to the nearest location

class ChargeOp(ABC):
    @abstractmethod
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        ...

class ChargeOpStart(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("START_CHARGE", {})

class ChargeOpStop(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("STOP_CHARGE", {})

class ChargeOpPortOpen(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHARGE_PORT_DOOR_OPEN", {})

class ChargeOpPortClose(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHARGE_PORT_DOOR_CLOSE", {})

class ChargeOpSetAmps(ChargeOp):
    amps: int

    def __init__(self, amps: int) -> None:
        self.amps = amps
        if amps < 0 or amps > 32:
            raise ArgException("Amps should be in range 0..32")

    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHARGING_AMPS", {"charging_amps": str(self.amps)})

class ChargeOpSetLimit(ChargeOp):
    percent: int

    def __init__(self, percent: int) -> None:
        self.percent = percent
        if percent < 0 or percent > 100:
            raise ArgException("Percentage should be in range 0..100")

    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHANGE_CHARGE_LIMIT", {"percent": str(self.percent)})

class ChargeOpSchedulingEnable(ChargeOp):
    minutes_past_midnight: int

    def __init__(self, minutes_past_midnight: int) -> None:
        self.minutes_past_midnight = minutes_past_midnight
        if minutes_past_midnight < 0 or minutes_past_midnight >= 24*60:
            raise ArgException("Scheduled time does not fall within the day")

    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("SCHEDULED_CHARGING", {"enable": True, "time": self.minutes_past_midnight})

class ChargeOpSchedulingDisable(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("SCHEDULED_CHARGING", {"enable": False, "time": None})

class AppState(StateElement):
    app: "App"

    def __init__(self, app: "App") -> None:
        self.app = app

    async def save(self, state: State) -> None:
        if not state.has_section("tesla"):
            state["tesla"] = {}
        state["tesla"]["location_detail"] = self.app.location_detail.value

        # TODO: move this to Control
        if not state.has_section("control"):
            state["control"] = {}
        state["control"]["require_bang"] = str(self.app.control.require_bang)

ClimateArgs = Tuple[Tuple[bool, Optional[VehicleName]], Tuple[()]]
def valid_on_off_vehicle(app: "App") -> p.Parser[ClimateArgs]:
    return p.Adjacent(p.Adjacent(p.Bool(), p.ValidOrMissing(ValidVehicle(app.tesla))),
                      p.Empty())

InfoArgs = Tuple[Optional[VehicleName], Tuple[()]]
def valid_info(app: "App") -> p.Parser[InfoArgs]:
    return p.Adjacent(p.ValidOrMissing(ValidVehicle(app.tesla)),
                      p.Empty())

LockUnlockArgs = Tuple[Optional[VehicleName], Tuple[()]]
def valid_lock_unlock(app: "App") -> p.Parser[LockUnlockArgs]:
    return p.Adjacent(p.ValidOrMissing(ValidVehicle(app.tesla)),
                      p.Empty())

ChargeArgs = Tuple[Tuple[ChargeOp, Optional[VehicleName]], Tuple[()]]
def valid_charge(app: "App") -> p.Parser[ChargeArgs]:
    return p.Adjacent(
        p.Adjacent(
            p.OneOf[ChargeOp](
                p.Map(parser=p.CaptureFixedStr("start"),
                      map=lambda _: ChargeOpStart()),
                p.Map(parser=p.CaptureFixedStr("stop"),
                      map=lambda _: ChargeOpStop()),
                p.Map(parser=p.Seq([p.CaptureFixedStr("port"), p.CaptureFixedStr("open")]),
                      map=lambda _: ChargeOpPortOpen()),
                p.Map(parser=p.Seq([p.CaptureFixedStr("port"), p.CaptureFixedStr("close")]),
                      map=lambda _: ChargeOpPortClose()),
                p.Map(parser=p.Keyword("amps", p.Int()),
                      map=ChargeOpSetAmps),
                p.Map(parser=p.Keyword("limit", p.Int()),
                      map=ChargeOpSetLimit),
                p.Map(parser=p.Keyword("schedule", p.CaptureFixedStr("disable")),
                      map=lambda x: ChargeOpSchedulingDisable()),
                p.Map(parser=p.Keyword("schedule", p.HhMm()),
                      map=lambda x: ChargeOpSchedulingEnable(x[0] * 60 + x[1])),
            ),
            p.ValidOrMissing(ValidVehicle(app.tesla))),
        p.Empty()
    )

class HeaterObject(ABC):
    @abstractmethod
    def get_command(self, heater_level: "HeaterLevel") -> Tuple[str, Dict[str, Any]]:
        ...

class HeaterSeat(HeaterObject):
    seat: int

    def __init__(self, seat: int) -> None:
        self.seat = seat
        if seat < 1 or seat > 6:
            raise ArgException("Seat should be in range 1..6")

    def get_command(self, heater_level: "HeaterLevel") -> Tuple[str, Dict[str, Any]]:
        return ("REMOTE_SEAT_HEATER_REQUEST",
                {"heater": self.seat - 1,
                 "level": heater_level.numeric()})

class HeaterSteering(HeaterObject):
    def get_command(self, heater_level: "HeaterLevel") -> Tuple[str, Dict[str, Any]]:
        return ("REMOTE_STEERING_WHEEL_HEATER_REQUEST",
                {"on": heater_level.binary()})

class HeaterLevel(Enum):
    Off    = "off"
    Low    = "low"
    Medium = "medium"
    High   = "high"

    def numeric(self) -> int:
        return {HeaterLevel.Off    : 0,
                HeaterLevel.Low    : 1,
                HeaterLevel.Medium : 2,
                HeaterLevel.High   : 3}[self]

    def binary(self) -> int:
        return {HeaterLevel.Off    : False,
                HeaterLevel.Low    : True,
                HeaterLevel.Medium : True,
                HeaterLevel.High   : True}[self]

HeaterArgs = Tuple[Tuple[Tuple[HeaterObject, HeaterLevel], Optional[VehicleName]], Tuple[()]]
def valid_heater(app: "App") -> p.Parser[HeaterArgs]:
    return p.Adjacent(
        p.Adjacent(
            p.Adjacent(
                p.OneOf[HeaterObject](
                    p.Map(parser=p.Keyword("seat", p.Int()),
                          map=HeaterSeat),
                    p.Map(parser=p.CaptureFixedStr("steering"),
                          map=lambda _: HeaterSteering())),
                p.OneOfEnumValue(HeaterLevel)
            ),
            p.ValidOrMissing(ValidVehicle(app.tesla))
        ),
        p.Empty())

ShareArgs = Tuple[Tuple[str, Optional[VehicleName]], Tuple[()]]
def valid_share(app: "App") -> p.Parser[ShareArgs]:
    return p.Adjacent(p.Adjacent(p.Concat(),
                                 p.ValidOrMissing(ValidVehicle(app.tesla))),
                      p.Empty())

def cmd_adjacent(label: str, parser: p.Parser[T]) -> p.Parser[Tuple[str, T]]:
    return p.Labeled(label=label, parser=p.Adjacent(p.CaptureFixedStr(label), parser).base())

SetArgs = Callable[[CommandContext], Awaitable[None]]
def SetArgsParser(app: "App") -> p.Parser[SetArgs]:
    return app._set_commands.parser()

def format_time(dt: datetime.datetime) -> str:
    return dt.strftime("%H:%M")

def format_hours(hours: float) -> str:
    h = math.floor(hours)
    m = math.floor((hours % 1.0) * 60.0)
    return f"{h}h{m}m"

def format_km(km: float) -> str:
    if km < 1:
        return f"{km * 1000:.0f} m"
    else:
        return f"{km:.2f} km"

def cache_load() -> Dict[str, Any]:
    cache: Dict[str, Any] = firestore.Client().collection(u'tesla').document(u'cache').get().to_dict()
    return cache

def cache_dump(cache: Dict[str, Any]) -> None:
    cache_doc = firestore.Client().collection(u'tesla').document(u'cache')
    cache_doc.set(cache)

class App(ControlCallback):
    control: Control
    config: Config
    state: State
    tesla: teslapy.Tesla
    _commands: commands.Commands[CommandContext]
    _set_commands: commands.Commands[CommandContext]
    _scheduler: AppScheduler[None]
    locations: Locations
    location_detail: LocationDetail

    def __init__(self,
                control: Control,
                env: Env) -> None:
        self.control = control
        self.config = env.config
        self.state = env.state
        self.locations = Locations(self.state)
        self.location_detail = LocationDetail.Full
        control.callback = self
        cache_loader: Union[Callable[[], Dict[str, Any]], None] = None
        cache_dumper: Union[Callable[[Dict[str, Any]], None], None] = None
        if self.config.get("common", "storage") == "cloud":
            cache_loader = cache_load
            cache_dumper = cache_dump
        cache_file=self.config.get("tesla", "credentials_store", fallback="cache.json")
        self.tesla = teslapy.Tesla(self.config.get("tesla", "email"),
                                   cache_file=cache_file,
                                   cache_dumper=cache_dumper,
                                   cache_loader=cache_loader)
        c = commands
        self._scheduler = AppScheduler(
            state=self.state,
            control=self.control,
            schedulable_commands=[
                cmd_adjacent("climate", valid_on_off_vehicle(self)).any(),
                cmd_adjacent("ac", valid_on_off_vehicle(self)).any(),
                cmd_adjacent("sauna", valid_on_off_vehicle(self)).any(),
                cmd_adjacent("info", valid_info(self)).any(),
                cmd_adjacent("lock", valid_lock_unlock(self)).any(),
                cmd_adjacent("unlock", valid_lock_unlock(self)).any(),
                cmd_adjacent("charge", valid_charge(self)).any(),
                cmd_adjacent("heater", valid_heater(self)).any(),
                cmd_adjacent("share", valid_share(self)).any(),
            ])
        self._commands = c.Commands()
        self._scheduler.register(self._commands)
        self._commands.register(c.Function("authorize", "Pass the Tesla API authorization URL",
                                           p.ValidOrMissing(p.Url()), self._command_authorized))
        self._commands.register(c.Function("vehicles", "List vehicles",
                                           p.Empty(), self._command_vehicles))
        self._commands.register(c.Function("climate", "climate on|off [vehicle] - control climate",
                                           valid_on_off_vehicle(self), self._command_climate))
        self._commands.register(c.Function("ac", "ac on|off [vehicle] - same as climate",
                                           valid_on_off_vehicle(self), self._command_climate))
        self._commands.register(c.Function("sauna", "sauna on|off [vehicle] - max defrost on/off",
                                           valid_on_off_vehicle(self), self._command_sauna))
        self._commands.register(c.Function("info", "info [vehicle] - Show vehicle location, temperature, etc",
                                           valid_info(self), self._command_info))
        self._commands.register(c.Function("lock", "lock [vehicle] - Lock vehicle doors",
                                           valid_lock_unlock(self), self._command_lock))
        self._commands.register(c.Function("unlock", "unlock [vehicle] - Unlock vehicle doors",
                                           valid_lock_unlock(self), self._command_unlock))
        self._commands.register(c.Function("charge", "charge (start|stop|amps nnn|limit nnn|port (open|close)|schedule (hh:mm|disable)) [vehicle] - Manage charging and charging port",
                                           valid_charge(self), self._command_charge))
        self._commands.register(c.Function("heater", "heater (seat (1..6)|steering) (off|low|medium|high) [vehicle] - Adjust seat and steering wheel heaters. Steering wheel heater can only be off or high.",
                                           valid_heater(self), self._command_heater))
        self._commands.register(c.Function("share", "Share an address on an URL with the vehicle",
                                           valid_share(self), self._command_share))
        self._commands.register(c.Function("location", f"location add|rm|ls\n{indent(2, self.locations.help())}",
                                           p.Remaining(LocationArgsParser(self.locations)),
                                           self._command_location))
        self._commands.register(c.Function("help", "Show help",
                                           p.Empty(), self._command_help))
        self._commands.register(c.Function("logout", "Log out from current user - authenticate again with !authorize",
                                           p.Empty(), self._command_logout))

        self._set_commands = c.Commands()
        self._set_commands.register(c.Function("location-detail", "full, near, at, nearest",
                                               p.OneOfEnumValue(LocationDetail), self._command_set_location_detail))
        # TODO: move this to Control
        self._set_commands.register(c.Function("require-!", "true or false, whether to require ! in front of commands",
                                               p.Remaining(p.Bool()), self._command_set_require_bang))

        self._commands.register(c.Function("set", f"Set a configuration parameter\n{indent(2, self._set_commands.help())}",
                                           SetArgsParser(self), self._command_set))

    async def _command_help(self, command_context: CommandContext, args: Tuple[()]) -> None:
        await self.control.send_message(command_context.to_message_context(),
                                        self._commands.help())

    async def _command_logout(self, context: CommandContext, args: Tuple[()]) -> None:
        if not self.tesla.authorized:
            await self.control.send_message(context.to_message_context(), "There is no user authorized! Please use !authorize.")
        elif not context.admin_room:
            await self.control.send_message(context.to_message_context(), "Please use the admin room for this command.")
        else:
            def call() -> None:
                self.tesla.logout()
            await to_async(call)
            await self.control.send_message(context.to_message_context(), "Logout successful!")

    async def command_callback(self,
                               command_context: CommandContext,
                               invocation: Invocation) -> None:
        """ControlCallback"""
        logger.debug(f"command_callback({invocation.name} {invocation.args})")
        if self._commands.has_command(invocation.name):
            try:
                await self._commands.invoke(command_context, invocation)
            except AppException as exn:
                logger.error(str(exn))
                await self.control.send_message(command_context.to_message_context(),
                                                exn.args[0])
            except commands.CommandsException as exn:
                raise exn
            except Exception as exn:
                logger.error(f"{command_context.txn} {exn} {traceback.format_exc()}")
                await self.control.send_message(command_context.to_message_context(),
                                                f"{command_context.txn} Exception :(")
        else:
            await self.control.send_message(command_context.to_message_context(), "No such command")

    async def _command_location(self, context: CommandContext, args: LocationArgs) -> None:
        class LocationCommandContext(LocationCommandContextBase):
            app: App
            def __init__(self, app: App, context: CommandContext) -> None:
                super().__init__(context=context)
                self.app = app

            async def get_location(self, vehicle_name: Optional[str]) -> Optional[LatLon]:
                def call(vehicle: teslapy.Vehicle) -> Any:
                    return vehicle.get_vehicle_data()
                data = await self.app._command_on_vehicle(context, vehicle_name, call, show_success=False)
                if data:
                    lat = data["drive_state"]["latitude"]
                    lon = data["drive_state"]["longitude"]
                    return LatLon(lat, lon)
                else:
                    return None
        await self.locations.command(LocationCommandContext(self, context), args)

    async def _command_share(self, context: CommandContext, args: ShareArgs) -> None:
        (url_or_address, vehicle_name), _ = args
        command = "SEND_TO_VEHICLE"
        logger.debug(f"Sending {command}")
        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command,
                                   type="share_ext_content_raw",
                                   #locale="en-US",
                                   locale="fi", # https://www.andiamo.co.uk/resources/iso-language-codes/
                                   timestamp_ms=int(time.time()),
                                   value={"android.intent.extra.TEXT": url_or_address})
        await self._command_on_vehicle(context, vehicle_name, call)
        pass

    async def _command_set(self, context: CommandContext, args: SetArgs) -> None:
        await args(context)

    async def _command_set_location_detail(self, context: CommandContext, args: LocationDetail) -> None:
        self.location_detail = args
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        f"Location detail set to {self.location_detail.value}")

    # TODO: move this to Control
    async def _command_set_require_bang(self, context: CommandContext, args: bool) -> None:
        self.control.require_bang = args
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        f"Require bang set to {self.control.require_bang}")

    async def _command_authorized(self, context: CommandContext, authorization_response: Optional[str]) -> None:
        if self.tesla.authorized:
            await self.control.send_message(context.to_message_context(), "Already authorized!")
        elif not context.admin_room:
            await self.control.send_message(context.to_message_context(), "Please use the admin room for this command.")
        else:
            if authorization_response is not None:
                # https://github.com/python/mypy/issues/9590
                def call() -> None:
                    self.tesla.fetch_token(authorization_response=authorization_response)
                await to_async(call)
                await self.control.send_message(context.to_message_context(), "Authorization successful")
                vehicles = self.tesla.vehicle_list()
                await self.control.send_message(context.to_message_context(), str(vehicles[0]))
            elif not self.tesla.authorized:
                await self.control.send_message(context.to_message_context(), f"Not authorized. Authorization URL: {self.tesla.authorization_url()} \"Page Not Found\" will be shown at success. Use !authorize https://the/url/you/ended/up/at")


    async def _command_vehicles(self, context: CommandContext, valid: Tuple[()]) -> None:
        vehicles = self.tesla.vehicle_list()
        await self.control.send_message(context.to_message_context(), f"vehicles: {vehicles}")

    async def _get_vehicle(self, display_name: Optional[str]) -> teslapy.Vehicle:
        vehicles = self.tesla.vehicle_list()
        if display_name is not None:
            vehicles = [vehicle for vehicle in vehicles if vehicle["display_name"].lower() == display_name.lower()]
        if len(vehicles) > 1:
            raise ArgException("Matched more than one vehicle; aborting")
        elif len(vehicles) == 0:
            if display_name is not None:
                raise ArgException(f"No vehicle found by name {display_name}")
            else:
                raise ArgException(f"No vehicle found")
        else:
            logger.debug(f"vehicle={vehicles[0]}")
            return vehicles[0]

    async def _wake(self, context: CommandContext, vehicle: teslapy.Vehicle) -> None:
        async def report() -> None:
            await self.control.send_message(context.to_message_context(), f"Waking up {vehicle['display_name']}")
        try:
            await call_with_delay_info(delay_sec=2.0,
                                       report=report,
                                       task=to_async(vehicle.sync_wake_up))
        except teslapy.VehicleError as exn:
            raise VehicleException(f"Failed to wake up vehicle; aborting")

    async def _load_state(self) -> None:
        if self.state.has_section("tesla"):
            location_detail_value = self.state.get("tesla", "location_detail", fallback=LocationDetail.Full.value)
            matching_location_details = [enum for enum in LocationDetail.__members__.values() if enum.value == location_detail_value]
            self.location_detail = matching_location_details[0]

        # TODO: move this to Control
        if self.state.has_section("control"):
            self.control.require_bang = bool(self.state.get("control", "require_bang", fallback=str(self.control.require_bang)) == str(True))

    def format_location(self, location: Location) -> str:
        nearest_name, nearest = self.locations.nearest_location(location)
        near_threshold = coalesce(location.near_km, DEFAULT_NEAR_THRESHOLD_KM)
        # TODO: but there could be another location that's not the nearest, but has
        # a larger near_km..
        distance = location.km_to(nearest) if nearest is not None else None
        near = nearest and (distance < near_threshold if distance is not None else False)
        # just so we need to check less stuff in the code..
        distance_str = f"{format_km(distance)}" if distance is not None else ""
        if self.location_detail == LocationDetail.Full:
            # show precise location information
            st = f"{location} {location.url()}"
            if nearest_name is not None:
                st += f" {distance_str} to {nearest_name}"
            return st
        else:
            if self.location_detail == LocationDetail.Near:
                # show precise location is near some predefined location
                if near:
                    return f"{location} {location.url()} {distance_str} to {nearest_name}"
                else:
                    return f""
            elif self.location_detail == LocationDetail.At:
                # show only if location is near some predefined location
                if near:
                    return f"{distance_str} to {nearest_name}"
                else:
                    return ""
            elif self.location_detail == LocationDetail.Nearest:
                # show distance to the nearest location
                if nearest:
                    return f"{distance_str} to {nearest_name}"
                else:
                    return f""
            else:
                assert False


    async def _command_info(self, context: CommandContext, args: InfoArgs) -> None:
        vehicle_name, _ = args
        try:
            def call(vehicle: teslapy.Vehicle) -> Any:
                return vehicle.get_vehicle_data()
            data = await self._command_on_vehicle(context, vehicle_name, call, show_success=False)
            if not data:
                return
            logger.debug(f"data: {data}")
            dist_hr_unit        = data["gui_settings"]["gui_distance_units"]
            dist_unit           = assert_some(re.match(r"^[^/]*", dist_hr_unit), "Expected to find / from dist_hr_unit")[0]
            temp_unit           = data["gui_settings"]["gui_temperature_units"]
            gps_as_of           = data["drive_state"]["gps_as_of"]
            heading             = data["drive_state"]["heading"]
            lat                 = data["drive_state"]["latitude"]
            lon                 = data["drive_state"]["longitude"]
            speed               = data["drive_state"]["speed"]
            battery_level       = data["charge_state"]["battery_level"]
            battery_range       = data["charge_state"]["battery_range"]
            est_battery_range   = data["charge_state"]["est_battery_range"]
            charge_limit        = data["charge_state"]["charge_limit_soc"]
            charge_current_request = data["charge_state"]["charge_current_request"]
            scheduled_charging_mode = data["charge_state"]["scheduled_charging_mode"]
            scheduled_charging_start_time : Optional[float] = data["charge_state"]["scheduled_charging_start_time"]
            charge_rate         = data["charge_state"]["charge_rate"]
            charging_state      = data["charge_state"]["charging_state"]
            time_to_full_charge = data["charge_state"]["time_to_full_charge"]
            car_version         = data["vehicle_state"]["car_version"]
            front_trunk_open    = data["vehicle_state"]["ft"] != 0
            rear_trunk_open     = data["vehicle_state"]["rt"] != 0
            locked              = data["vehicle_state"]["locked"]
            front_driver_window = data["vehicle_state"]["fd_window"] != 0
            front_passanger_window = data["vehicle_state"]["fp_window"] != 0
            rear_driver_window  = data["vehicle_state"]["rd_window"] != 0
            rear_passanger_window = data["vehicle_state"]["rp_window"] != 0
            valet_mode          = data["vehicle_state"]["valet_mode"]
            odometer            = int(data["vehicle_state"]["odometer"])
            display_name        = data["vehicle_state"]["vehicle_name"]
            climate_state       = data["climate_state"]
            inside_temp         = climate_state.get("inside_temp")
            outside_temp        = climate_state.get("outside_temp")
            seat_heater_left    = climate_state.get("seat_heater_left")
            seat_heater_right   = climate_state.get("seat_heater_right")
            seat_heater_rear_center = climate_state.get("seat_heater_rear_center")
            seat_heater_rear_left = climate_state.get("seat_heater_rear_left")
            seat_heater_rear_right = climate_state.get("seat_heater_rear_right")
            message = f"{display_name} version {car_version}\n"
            seat_heaters_str = ', '.join([str(x) for x in [seat_heater_left, seat_heater_right, \
                                                           seat_heater_rear_left, seat_heater_rear_center, \
                                                           seat_heater_rear_right]])
            message += f"Inside: {inside_temp}°{temp_unit} Outside: {outside_temp}°{temp_unit} Seat heaters: {seat_heaters_str}\n"
            message += f"Heading: {heading} " + self.format_location(Location(lat=lat, lon=lon)) + f" Speed: {speed}\n"
            message += f"Battery: {battery_level}% {battery_range} {dist_unit} est. {est_battery_range} {dist_unit}\n"
            charge_eta = datetime.datetime.now() + datetime.timedelta(hours=time_to_full_charge)
            message += f"Charge limit: {charge_limit}%"
            message += f" Charge current limit: {charge_current_request}A";
            if charge_rate or charging_state == "Charging":
                message += f" Charge rate: {charge_rate}A";
                if time_to_full_charge > 0:
                    message += f" Ready at: {format_time(charge_eta)} (+{format_hours(time_to_full_charge)})"
                else:
                    message += f" Ready at: unknown"
            if scheduled_charging_mode == "StartAt":
                message += \
                    "\nCharging scheduled to start at " + \
                    str(map_optional(
                        scheduled_charging_start_time,
                        lambda x: datetime.datetime.fromtimestamp(x).strftime('%Y-%m-%d %H:%M')
                    ))
            message += f"\nOdometer: {odometer} {dist_unit}"
            message += f"\nVehicle is {'locked' if locked else 'unlocked'}"
            if valet_mode:
                message += f"\nValet mode enabled"
            if front_trunk_open:
                message += f"\nFrunk open"
            if rear_trunk_open:
                message += f"\nTrunk open"
            if front_driver_window:
                message += f"\nFront driver window open"
            if front_passanger_window:
                message += f"\nFront passanger window open"
            if rear_driver_window:
                message += f"\nRear driver window open"
            if rear_passanger_window:
                message += f"\nRear passanger window open"
            await self.control.send_message(context.to_message_context(),
                                            message)
        except HTTPError as exn:
            await self.control.send_message(context.to_message_context(), str(exn))

    async def _command_lock(self, context: CommandContext, args: LockUnlockArgs) -> None:
        vehicle_name, _ = args
        command = "LOCK"
        logger.debug(f"Sending {command}")
        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command)
        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_unlock(self, context: CommandContext, args: LockUnlockArgs) -> None:
        vehicle_name, _ = args
        command = "UNLOCK"
        logger.debug(f"Sending {command}")
        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command)
        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_charge(self, context: CommandContext, args: ChargeArgs) -> None:
        (charge_op, vehicle_name), _ = args
        command, kwargs = charge_op.get_command()
        logger.debug(f"Sending {command} {kwargs}")
        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command, **kwargs)
        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_heater(self, context: CommandContext, args: HeaterArgs) -> None:
        ((heater_object, heater_level), vehicle_name), _ = args
        command, kwargs = heater_object.get_command(heater_level)
        logger.debug(f"Sending {command} {kwargs}")
        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command, **kwargs)
        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_on_vehicle(self,
                                  context: CommandContext,
                                  vehicle_name: Optional[str],
                                  fn: Callable[[teslapy.Vehicle], T],
                                  show_success: bool = True) -> Optional[T]:
        vehicle = await self._get_vehicle(vehicle_name)
        await self._wake(context, vehicle)
        num_retries = 0
        error = None
        result: Optional[T] = None
        while num_retries < 5:
            try:
                # https://github.com/python/mypy/issues/9590
                def call() -> Any:
                    return fn(vehicle)
                result = await to_async(call)
                error = None
                break
            except teslapy.VehicleError as exn:
                logger.debug(f"Vehicle error: {exn}")
                error = exn
                if exn.args[0] != "could_not_wake_buses":
                    break
            except HTTPError as exn:
                logger.debug(f"HTTP error: {exn}")
                error = exn
            finally:
                logger.debug(f"Done sending")
            await asyncio.sleep(pow(1.15, num_retries) * 2)
            num_retries += 1
        if error:
            await self.control.send_message(context.to_message_context(), f"Error: {error}")
            return None
        else:
            assert result is not None
            if show_success:
                message = "Success!"
                if result != True: # this never happens, though?
                    message += f" {result}"
                await self.control.send_message(context.to_message_context(), message)
            return result

    async def _command_climate(self, context: CommandContext, args: ClimateArgs) -> None:
        (mode, vehicle_name), _ = args
        command = "CLIMATE_ON" if mode else "CLIMATE_OFF"
        logger.debug(f"Sending {command}")
        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command)
        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_sauna(self, context: CommandContext, args: ClimateArgs) -> None:
        (mode, vehicle_name), _ = args
        command = "MAX_DEFROST"
        logger.debug(f"Sending {command} {mode}")
        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command, on=mode)
        await self._command_on_vehicle(context, vehicle_name, call)

    async def run(self) -> None:
        await self._scheduler.start()
        await self._load_state()
        await self.control.send_message(MessageContext(admin_room=False), f"TeslaBot {__version__} started")
        self.state.add_element(AppState(self))

        if not self.tesla.authorized:
            await self.control.send_message(MessageContext(admin_room=True), f"Not authorized. Authorization URL: {self.tesla.authorization_url()} \"Page Not Found\" will be shown at success. Use !authorize https://the/url/you/ended/up/at")
