import asyncio
from typing import List, Optional, Tuple, Generic, Callable, Awaitable, cast, Any, Union
import re
import datetime
from configparser import ConfigParser
from dataclasses import dataclass
import traceback
import json
from enum import Enum
import math

import teslapy
from urllib.error import HTTPError

from .control import Control, ControlCallback, CommandContext, MessageContext
from .commands import Invocation
from . import log
from .config import Config
from .state import State, StateElement
from . import commands
from . import parser as p
from .utils import assert_some, indent
from . import scheduler
from .env import Env
from .locations import Location, Locations, LocationArgsParser
from .asyncthread import to_async
from . import __version__

logger = log.getLogger(__name__)

DISTANCE_THRESHOLD_KM = 0.5

class AppException(Exception):
    pass

class ArgException(AppException):
    pass

class VehicleException(AppException):
    pass

class ValidVehicle(p.Delayed[str]):
    tesla: teslapy.Tesla

    def __init__(self, tesla: teslapy.Tesla) -> None:
        super().__init__(self.make_validator)
        self.tesla = tesla

    def make_validator(self) -> p.Parser[str]:
        vehicles = self.tesla.vehicle_list()
        display_names = [vehicle["display_name"] for vehicle in vehicles]
        return p.OneOfStrings(display_names)

@dataclass
class AppTimerInfo:
    id: int
    command: List[str]
    def json(self) -> Any:
        # don't serialize id, as it will be the key
        return {"command": self.command}

    @staticmethod
    def from_json(id: int, json: Any) -> "AppTimerInfo":
        return AppTimerInfo(id=id,
                            command=json["command"])

class AppTimerInfoBase:
    info: AppTimerInfo

    def __init__(self, info: AppTimerInfo) -> None:
        self.info = info

    def json(self) -> Any:
        return {"info": self.info.json()}

class AppOneShot(scheduler.OneShot, AppTimerInfoBase):
    def __init__(self,
                 callback: Callable[[], Awaitable[None]],
                 time: datetime.datetime,
                 info: AppTimerInfo) -> None:
        scheduler.OneShot.__init__(self, callback, time)
        AppTimerInfoBase.__init__(self, info)

    def json(self) -> Any:
        base = AppTimerInfoBase.json(self)
        base["time"] = self.time.isoformat()
        return base

    @staticmethod
    def from_json(id: int, json: Any, callback: Callable[[scheduler.Entry], Awaitable[None]]) -> scheduler.Entry:
        async def indirect_callback() -> None:
            await callback(entry)
        entry = AppOneShot(callback=indirect_callback,
                           time=datetime.datetime.fromisoformat(json["time"]),
                           info=AppTimerInfo.from_json(id, json["info"]))
        return entry

class LocationDetail(Enum):
    Full = "full"       # show precise location information
    Near = "near"       # show precise location is near some predefined location
    At = "at"           # show only if location is near some predefined location
    Nearest = "nearest" # show distance to the nearest location

class AppState(StateElement):
    app: "App"

    def __init__(self, app: "App") -> None:
        self.app = app

    async def save(self, state: ConfigParser) -> None:
        entries = await self.app._scheduler.get_entries()
        if not "tesla.timers" in state:
            state["tesla.timers"] = {}
        timers = state["tesla.timers"]
        timers.clear()
        for entry in entries:
            if isinstance(entry, AppOneShot):
                timers[str(entry.info.id)] = json.dumps(entry.json())
        if not "tesla" in state:
            state["tesla"] = {}
        state["tesla"]["location_detail"] = self.app.location_detail.value

        # TODO: move this to Control
        if not "control" in state:
            state["control"] = {}
        state["control"]["require_bang"] = str(self.app.control.require_bang)

ClimateArgs = Tuple[Tuple[bool, Optional[str]], Tuple[()]]
def valid_climate(app: "App") -> p.Parser[ClimateArgs]:
    return p.Adjacent(p.Adjacent(p.Bool(), p.ValidOrMissing(ValidVehicle(app.tesla))),
                      p.Empty())

InfoArgs = Tuple[Optional[str], Tuple[()]]
def valid_info(app: "App") -> p.Parser[InfoArgs]:
    return p.Adjacent(p.ValidOrMissing(ValidVehicle(app.tesla)),
                      p.Empty())

CommandWithArgs = List[str]
def valid_schedulable(app: "App") -> p.Parser[CommandWithArgs]:
    cmds = [
        p.Adjacent(p.FixedStr("climate"), valid_climate(app)).any(),
        p.Adjacent(p.FixedStr("info"), valid_info(app)).any(),
    ]
    return p.CaptureOnly(p.OneOf(cmds))

SetArgs = Callable[[CommandContext], Awaitable[None]]
def SetArgsParser(app: "App") -> p.Parser[SetArgs]:
    return app._set_commands.parser()

def valid_command(cmds: List[commands.Function[CommandContext, Any]]) -> p.Parser[CommandWithArgs]:
    cmd_parsers = [p.Adjacent(p.FixedStr(cmd.name), cmd.parser).any() for cmd in cmds]
    return p.CaptureOnly(p.OneOf(cmd_parsers))

def format_hours(hours: float) -> str:
    h = math.floor(hours)
    m = math.floor((hours % 1.0) * 60.0)
    return f"{h}h{m}m"

class App(ControlCallback):
    control: Control
    config: Config
    state: State
    tesla: teslapy.Tesla
    _commands: commands.Commands[CommandContext]
    _set_commands: commands.Commands[CommandContext]
    _scheduler: scheduler.Scheduler
    _scheduler_id: int
    locations: Locations
    location_detail: LocationDetail

    def __init__(self, control: Control, env: Env) -> None:
        self.control = control
        self.config = env.config
        self.state = env.state
        self.locations = Locations(self.state)
        self.location_detail = LocationDetail.Full
        control.callback = self
        self._scheduler = scheduler.Scheduler()
        cache_file=self.config.get("tesla", "credentials_store", fallback="cache.json")
        self.tesla = teslapy.Tesla(self.config.get("tesla", "email"),
                                   cache_file=cache_file)
        self._scheduler_id = 1
        c = commands
        self._commands = c.Commands()
        self._commands.register(c.Function("authorize", "Pass the Tesla API authorization URL",
                                           p.AnyStr(), self._command_authorized))
        self._commands.register(c.Function("vehicles", "List vehicles",
                                           p.Empty(), self._command_vehicles))
        self._commands.register(c.Function("climate", "climate on|off [vehicle] - control climate",
                                           valid_climate(self), self._command_climate))
        self._commands.register(c.Function("info", "info [vehicle] - Show vehicle location, temperature, etc",
                                           valid_info(self), self._command_info))
        self._commands.register(c.Function("at", "Schedule operation: at 06:00 climate on",
                                           p.Remaining(p.Adjacent(p.HourMinute(), valid_schedulable(self))), self._command_at))
        self._commands.register(c.Function("atrm", "Remove a scheduled operation or a running task by its identifier",
                                           p.Remaining(p.Int()), self._command_rm))
        self._commands.register(c.Function("atq", "List scheduled operations or running tasks",
                                           p.Empty(), self._command_ls))
        self._commands.register(c.Function("location", f"location add|rm|ls\n{indent(2, self.locations.help())}",
                                           p.Remaining(LocationArgsParser(self.locations)), self.locations.command))
        self._commands.register(c.Function("help", "Show help",
                                           p.Empty(), self._command_help))

        self._set_commands = c.Commands()
        self._set_commands.register(c.Function("location-detail", "full, near, at, nearest",
                                               p.OneOfEnumValue(LocationDetail), self._command_set_location_detail))
        # TODO: move this to Control
        self._set_commands.register(c.Function("require-!", "true or false, whether to require ! in front of commands",
                                               p.Remaining(p.Bool()), self._command_set_require_bang))

        self._commands.register(c.Function("set", f"Set a configuration parameter\n{indent(2, self._set_commands.help())}",
                                           SetArgsParser(self), self._command_set))

    def _next_scheduler_id(self) -> int:
        id = self._scheduler_id
        self._scheduler_id += 1
        return id

    async def _command_help(self, command_context: CommandContext, args: Tuple[()]) -> None:
        await self.control.send_message(command_context.to_message_context(),
                                        self._commands.help())

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

    async def _command_authorized(self, context: CommandContext, authorization_response: str) -> None:
        if not context.admin_room:
            await self.control.send_message(context.to_message_context(), "Please use the admin room for this command.")
        else:
            await self.control.send_message(context.to_message_context(), "Authorization successful")
            # https://github.com/python/mypy/issues/9590
            def call() -> None:
                self.tesla.fetch_token(authorization_response=authorization_response)
            await to_async(call)
            vehicles = self.tesla.vehicle_list()
            await self.control.send_message(context.to_message_context(), str(vehicles[0]))

    async def _command_ls(self, context: CommandContext, valid: Tuple[()]) -> None:
        entries = await self._scheduler.get_entries()
        if entries:
            result: List[str] = []
            for entry in entries:
                if isinstance(entry, AppOneShot):
                    info = entry.info
                    result.append(f"{info.id} {entry.time}: {' '.join(info.command)}")
            result_lines = "\n".join(result)
            await self.control.send_message(context.to_message_context(), f"Timers:\n{result_lines}")
        else:
            await self.control.send_message(context.to_message_context(), f"No timers set.")

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
            raise ArgException("No vehicle found")
        else:
            logger.debug(f"vehicle={vehicles[0]}")
            return vehicles[0]

    async def _wake(self, context: CommandContext, vehicle: teslapy.Vehicle) -> None:
        await self.control.send_message(context.to_message_context(), f"Waking up {vehicle['display_name']}")
        try:
            await to_async(vehicle.sync_wake_up)
        except teslapy.VehicleError as exn:
            raise VehicleException(f"Failed to wake up vehicle; aborting")

    async def _command_rm(self, context: CommandContext,
                          id: int) -> None:
        def matches(entry: scheduler.Entry) -> bool:
            if isinstance(entry, AppTimerInfoBase):
                logger.debug(f"Comparing {entry.info.id} vs {id}")
                return entry.info.id == id
            else:
                return False
        async def remove_entry(entries: List[scheduler.Entry]) -> Tuple[List[scheduler.Entry], bool]:
            new_entries = [entry for entry in entries if not matches(entry)]
            logger.debug(f"remove_entry: {entries} -> {new_entries}")
            return new_entries, len(new_entries) != len(entries)
        changed = await self._scheduler.with_entries(remove_entry)
        if changed:
            await self.state.save()
            await self.control.send_message(context.to_message_context(),
                                            f"Removed timer")
        else:
            await self.control.send_message(context.to_message_context(),
                                            f"No timers matched")

    async def _load_state(self) -> None:
        if self.state.state.has_section("tesla.timers"):
            for id, timer in self.state.state["tesla.timers"].items():
                self._scheduler_id = max(self._scheduler_id, int(id) + 1)
                entry = AppOneShot.from_json(int(id), json.loads(timer), callback=self._activate_timer)
                await self._scheduler.add(entry)
        if self.state.state.has_section("tesla"):
            location_detail_value = self.state.state.get("tesla", "location_detail", fallback=LocationDetail.Full.value)
            matching_location_details = [enum for enum in LocationDetail.__members__.values() if enum.value == location_detail_value]
            self.location_detail = matching_location_details[0]

        # TODO: move this to Control
        if self.state.state.has_section("control"):
            self.control.require_bang = bool(self.state.state.get("control", "require_bang", fallback=str(self.control.require_bang)) == str(True))

    async def _activate_timer(self, entry: scheduler.Entry) -> None:
        if isinstance(entry, AppOneShot):
            command = entry.info.command
            logger.info(f"Timer {entry.info.id} activated")
            await self._scheduler.remove(entry)
            await self.state.save()
            context = CommandContext(admin_room=False, control=self.control)
            await self.control.send_message(context.to_message_context(), f"Timer activated: \"{' '.join(command)}\"")
            invocation = commands.Invocation(name=command[0], args=command[1:])
            await self._commands.invoke(context, invocation)
            await self._command_ls(context, ())
        else:
            logger.info(f"Unknown timer {entry} activated..")

    async def _command_at(self, context: CommandContext,
                          args: Tuple[Tuple[int, int], CommandWithArgs]) -> None:
        hhmm, command = args

        async def callback() -> None:
            await self._activate_timer(entry)
        time_of_day = datetime.time(hhmm[0], hhmm[1])
        date = datetime.date.today()
        time = datetime.datetime.combine(date, time_of_day)
        while time < datetime.datetime.now():
            time += datetime.timedelta(days=1)
        scheduler_id = self._next_scheduler_id()
        entry = AppOneShot(callback, time, AppTimerInfo(id=scheduler_id, command=command))
        await self._scheduler.add(entry)
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        f"Scheduled \"{' '.join(command)}\" at {time} (id {scheduler_id})")

    def format_location(self, location: Location) -> str:
        nearest_name, nearest = self.locations.nearest_location(location)
        near = nearest and location.km_to(nearest) < DISTANCE_THRESHOLD_KM
        if self.location_detail == LocationDetail.Full:
            # show precise location information
            st = f"{location}"
            if nearest_name is not None:
                st += f" near {nearest_name}"
            return st
        else:
            if self.location_detail == LocationDetail.Near:
                # show precise location is near some predefined location
                if near:
                    return f"{location} near {nearest_name}"
                else:
                    return f""
            elif self.location_detail == LocationDetail.At:
                # show only if location is near some predefined location
                if near:
                    return f"near {nearest_name}"
                else:
                    return ""
            elif self.location_detail == LocationDetail.Nearest:
                # show distance to the nearest location
                if nearest:
                    assert nearest
                    return f"{nearest.km_to(location)}km to {nearest_name}"
                else:
                    return f""
            else:
                assert False


    async def _command_info(self, context: CommandContext, args: InfoArgs) -> None:
        vehicle_name, _ = args
        vehicle = await self._get_vehicle(vehicle_name)
        await self._wake(context, vehicle)
        try:
            data = await to_async(vehicle.get_vehicle_data)
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
            charge_rate         = data["charge_state"]["charge_rate"]
            time_to_full_charge = data["charge_state"]["time_to_full_charge"]
            odometer            = int(data["vehicle_state"]["odometer"])
            inside_temp         = data["climate_state"]["inside_temp"]
            outside_temp        = data["climate_state"]["outside_temp"]
            message = f"{vehicle['display_name']}\n"
            message += f"Inside: {inside_temp}°{temp_unit} Outside: {outside_temp}°{temp_unit}\n"
            message += f"Heading: {heading} " + self.format_location(Location(lat=lat, lon=lon)) + f" Speed: {speed}\n"
            message += f"Battery: {battery_level}% {battery_range} {dist_unit} est. {est_battery_range} {dist_unit}\n"
            message += f"Charge limit: {charge_limit}% Charge rate: {charge_rate}A Time to limit: {format_hours(time_to_full_charge)}\n"
            message += f"Odometer: {odometer} {dist_unit}"
            await self.control.send_message(context.to_message_context(),
                                            message)
        except HTTPError as exn:
            await self.control.send_message(context.to_message_context(), str(exn))


    async def _command_climate(self, context: CommandContext, args: ClimateArgs) -> None:
        (mode, vehicle_name), _ = args
        vehicle = await self._get_vehicle(vehicle_name)
        await self._wake(context, vehicle)
        num_retries = 0
        error = None
        result = None
        await self.control.send_message(context.to_message_context(), f"Sending command")
        while num_retries < 5:
            try:
                command = "CLIMATE_ON" if mode else "CLIMATE_OFF"
                logger.debug(f"Sending {command}")
                # https://github.com/python/mypy/issues/9590
                def call() -> Any:
                    vehicle.command(command)
                result = to_async(call)
                error = None
                break
            except teslapy.VehicleError as exn:
                logger.debug(f"Vehicle error: {exn}")
                error = exn
            except HTTPError as exn:
                logger.debug(f"HTTP error: {exn}")
                error = exn
            finally:
                logger.debug(f"Done sending")
            await asyncio.sleep(pow(1.15, num_retries) * 2)
            num_retries += 1
        if error:
            await self.control.send_message(context.to_message_context(), f"Error: {error}")
        else:
            assert result
            await self.control.send_message(context.to_message_context(), f"Success: {result}")

    async def run(self) -> None:
        await self._scheduler.start()
        await self._load_state()
        await self.control.send_message(MessageContext(admin_room=False), f"TeslaBot {__version__} started")
        self.state.add_element(AppState(self))
        if not self.tesla.authorized:
            await self.control.send_message(MessageContext(admin_room=True), f"Not authorized. Authorization URL: {self.tesla.authorization_url()} \"Page Not Found\" will be shown at success. Use !authorize https://the/url/you/ended/up/at")
