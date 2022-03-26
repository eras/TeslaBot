import asyncio
from typing import List, Optional, Tuple, Generic, Callable, Awaitable, cast, Any, Union, TypeVar
import re
import datetime
from configparser import ConfigParser
from dataclasses import dataclass
import traceback
import json
from enum import Enum
import math
import time

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
from . import scheduler
from .env import Env
from .locations import Location, Locations, LocationArgs, LocationArgsParser, LocationCommandContextBase, LocationInfoCoords, LatLon
from .asyncthread import to_async
from . import __version__

logger = log.getLogger(__name__)

T = TypeVar('T')

DEFAULT_NEAR_THRESHOLD_KM = 0.5

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
    until: Optional[datetime.datetime]

    def json(self) -> Any:
        # don't serialize id, as it will be the key
        j: Any = {"command": self.command}
        if self.until:
            j["until"] = self.until.isoformat()
        return j

    @staticmethod
    def from_json(id: int, json: Any) -> "AppTimerInfo":
        return AppTimerInfo(id=id,
                            command=json["command"],
                            until=datetime.datetime.fromisoformat(json["until"]) if "until" in json else None)

@dataclass
class SchedulerContext:
    info: AppTimerInfo

    def json(self) -> Any:
        return {"info": self.info.json()}

def timer_entry_to_json(entry: scheduler.Entry[SchedulerContext]) -> Any:
    base = entry.context.json()
    if isinstance(entry, scheduler.OneShot):
        base["time"] = entry.time.isoformat()
    elif isinstance(entry, scheduler.Periodic):
        base["next_time"] = entry.next_time.isoformat()
        base["interval_seconds"] = entry.interval.total_seconds()
    else:
        assert False, "Unsupported timer"
    return base

def timer_entry_from_json(id: int, json: Any, callback: Callable[[scheduler.Entry[SchedulerContext]], Awaitable[None]]) -> scheduler.Entry[SchedulerContext]:
    async def indirect_callback() -> None:
        await callback(entry)
    if "interval_seconds" in json:
        entry: scheduler.Entry[SchedulerContext] = \
            scheduler.Periodic(callback=indirect_callback,
                               time=datetime.datetime.fromisoformat(json["next_time"]),
                               interval=datetime.timedelta(seconds=json["interval_seconds"]),
                               context=SchedulerContext(info=AppTimerInfo.from_json(id, json["info"])))
    else:
        entry = scheduler.OneShot(callback=indirect_callback,
                                  time=datetime.datetime.fromisoformat(json["time"]),
                                  context=SchedulerContext(info=AppTimerInfo.from_json(id, json["info"])))
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
            timers[str(entry.context.info.id)] = json.dumps(timer_entry_to_json(entry))
        if not "tesla" in state:
            state["tesla"] = {}
        state["tesla"]["location_detail"] = self.app.location_detail.value

        # TODO: move this to Control
        if not "control" in state:
            state["control"] = {}
        state["control"]["require_bang"] = str(self.app.control.require_bang)

ClimateArgs = Tuple[Tuple[bool, Optional[str]], Tuple[()]]
def valid_on_off_vehicle(app: "App") -> p.Parser[ClimateArgs]:
    return p.Adjacent(p.Adjacent(p.Bool(), p.ValidOrMissing(ValidVehicle(app.tesla))),
                      p.Empty())

InfoArgs = Tuple[Optional[str], Tuple[()]]
def valid_info(app: "App") -> p.Parser[InfoArgs]:
    return p.Adjacent(p.ValidOrMissing(ValidVehicle(app.tesla)),
                      p.Empty())

LockUnlockArgs = Tuple[Optional[str], Tuple[()]]
def valid_lock_unlock(app: "App") -> p.Parser[LockUnlockArgs]:
    return p.Adjacent(p.ValidOrMissing(ValidVehicle(app.tesla)),
                      p.Empty())

ShareArgs = Tuple[Tuple[str, Optional[str]], Tuple[()]]
def valid_share(app: "App") -> p.Parser[ShareArgs]:
    return p.Adjacent(p.Adjacent(p.Concat(),
                                 p.ValidOrMissing(ValidVehicle(app.tesla))),
                      p.Empty())

CommandWithArgs = List[str]
def valid_schedulable(app: "App",
                      include_every: bool,
                      include_until: bool) -> p.Parser[CommandWithArgs]:
    cmds = [
        p.Adjacent(p.CaptureFixedStr("climate"), valid_on_off_vehicle(app)).any(),
        p.Adjacent(p.CaptureFixedStr("ac"), valid_on_off_vehicle(app)).any(),
        p.Adjacent(p.CaptureFixedStr("sauna"), valid_on_off_vehicle(app)).any(),
        p.Adjacent(p.CaptureFixedStr("info"), valid_info(app)).any(),
        p.Adjacent(p.CaptureFixedStr("lock"), valid_lock_unlock(app)).any(),
        p.Adjacent(p.CaptureFixedStr("unlock"), valid_lock_unlock(app)).any(),
        p.Adjacent(p.CaptureFixedStr("share"), valid_share(app)).any(),
    ]
    if include_every:
        cmds.append(p.Adjacent(p.CaptureFixedStr("every"),
                               valid_schedule_every(app,
                                                    include_until=include_until)).any())
    if include_until:
        cmds.append(p.Adjacent(p.CaptureFixedStr("until"),
                               valid_schedule_until(app,
                                                    include_every=include_every)).any())
    return p.CaptureOnly(p.OneOf(*cmds))

SetArgs = Callable[[CommandContext], Awaitable[None]]
def SetArgsParser(app: "App") -> p.Parser[SetArgs]:
    return app._set_commands.parser()

def valid_command(cmds: List[commands.Function[CommandContext, Any]]) -> p.Parser[CommandWithArgs]:
    cmd_parsers = [p.Adjacent(p.CaptureFixedStr(cmd.name), cmd.parser).any() for cmd in cmds]
    return p.CaptureOnly(p.OneOf(*cmd_parsers))

ScheduleAtArgs = Tuple[datetime.datetime,
                       CommandWithArgs]
def valid_schedule_at(app: "App") -> p.Parser[ScheduleAtArgs]:
    return p.Remaining(p.Adjacent(p.Time(), valid_schedulable(app, include_every=True, include_until=True)))

ScheduleEveryArgs = Tuple[Tuple[datetime.timedelta,
                                Optional[datetime.datetime]],
                          CommandWithArgs]
def valid_schedule_every(app: "App", include_until: bool) -> p.Parser[ScheduleEveryArgs]:
    return p.Remaining(p.Adjacent(p.Adjacent(p.Interval(),
                                             p.Optional_(p.Conditional(lambda: include_until,
                                                                       p.Keyword("until", p.Time())))),
                                  valid_schedulable(app, include_every=False, include_until=include_until)))

ScheduleUntilArgs = Tuple[Tuple[datetime.datetime,
                                Optional[datetime.timedelta]],
                          CommandWithArgs]
def valid_schedule_until(app: "App", include_every: bool) -> p.Parser[ScheduleUntilArgs]:
    return p.Remaining(p.Adjacent(p.Adjacent(p.Time(),
                                             p.Optional_(p.Conditional(lambda: include_every,
                                                                       p.Keyword("every", p.Interval())))),
                                  valid_schedulable(app, include_until=False, include_every=include_every)))

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

class App(ControlCallback):
    control: Control
    config: Config
    state: State
    tesla: teslapy.Tesla
    _commands: commands.Commands[CommandContext]
    _set_commands: commands.Commands[CommandContext]
    _scheduler: scheduler.Scheduler[SchedulerContext]
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
        self._commands.register(c.Function("at", "Schedule operation: at 06:00 climate on or at 1h30m every 10m info",
                                           valid_schedule_at(self), self._command_at))
        self._commands.register(c.Function("every", "Schedule operation: every 10m info",
                                           valid_schedule_every(self, include_until=True), self._command_every))
        self._commands.register(c.Function("until", "Schedule operation: until 10:00 info",
                                           valid_schedule_until(self, include_every=True), self._command_until))
        self._commands.register(c.Function("atrm", "Remove a scheduled operation or a running task by its identifier",
                                           p.Remaining(p.Int()), self._command_rm))
        self._commands.register(c.Function("atq", "List scheduled operations or running tasks",
                                           p.Empty(), self._command_ls))
        self._commands.register(c.Function("share", "Share an address on an URL with the vehicle",
                                           valid_share(self), self._command_share))
        self._commands.register(c.Function("location", f"location add|rm|ls\n{indent(2, self.locations.help())}",
                                           p.Remaining(LocationArgsParser(self.locations)),
                                           self._command_location))
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
                info = entry.context.info
                if isinstance(entry, scheduler.OneShot):
                    result.append(f"{info.id} {entry.time}: {' '.join(info.command)}")
                if isinstance(entry, scheduler.Periodic):
                    until = f" until {info.until}" if info.until else ""
                    result.append(f"{info.id} {entry.next_time}, repeats every {entry.interval}{until}: {' '.join(info.command)}")
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

    async def _command_rm(self, context: CommandContext,
                          id: int) -> None:
        def matches(entry: scheduler.Entry[SchedulerContext]) -> bool:
            logger.debug(f"Comparing {entry.context.info.id} vs {id}")
            return entry.context.info.id == id
        async def remove_entry(entries: List[scheduler.Entry[SchedulerContext]]) -> Tuple[List[scheduler.Entry[SchedulerContext]], bool]:
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
                entry = timer_entry_from_json(int(id), json.loads(timer), callback=self._activate_timer)
                await self._scheduler.add(entry)
        if self.state.state.has_section("tesla"):
            location_detail_value = self.state.state.get("tesla", "location_detail", fallback=LocationDetail.Full.value)
            matching_location_details = [enum for enum in LocationDetail.__members__.values() if enum.value == location_detail_value]
            self.location_detail = matching_location_details[0]

        # TODO: move this to Control
        if self.state.state.has_section("control"):
            self.control.require_bang = bool(self.state.state.get("control", "require_bang", fallback=str(self.control.require_bang)) == str(True))

    async def _activate_timer(self, entry: scheduler.Entry[SchedulerContext]) -> None:
        info = entry.context.info
        command = info.command
        logger.info(f"Timer {info.id} activated")
        next_time = entry.when_is_next(time.time())
        if isinstance(entry, scheduler.OneShot) or \
           (info.until is not None \
            and next_time is not None \
            and next_time > info.until.timestamp()):
            await self._scheduler.remove(entry)
        await self.state.save()
        context = CommandContext(admin_room=False, control=self.control)
        await self.control.send_message(context.to_message_context(), f"Timer activated: \"{' '.join(command)}\"")
        invocation = commands.Invocation(name=command[0], args=command[1:])
        await self._commands.invoke(context, invocation)
        await self._command_ls(context, ())

    async def _command_every(self, context: CommandContext,
                             args: ScheduleEveryArgs) -> None:
        (interval, until), command = args
        async def callback() -> None:
            await self._activate_timer(entry)
        scheduler_id = self._next_scheduler_id()
        app_timer_info = AppTimerInfo(id=scheduler_id,
                                      command=command,
                                      until=until)
        sched_context = SchedulerContext(info=app_timer_info)
        message = f"Repeat every {interval}"
        entry = scheduler.Periodic(callback,
                                   time=round_to_next_second(datetime.datetime.now()),
                                   interval=interval,
                                   context=sched_context)
        await self._scheduler.add(entry)
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        message)

    async def _command_until(self, context: CommandContext,
                             args: ScheduleUntilArgs) -> None:
        (until, interval), command = args
        async def callback() -> None:
            await self._activate_timer(entry)
        scheduler_id = self._next_scheduler_id()
        app_timer_info = AppTimerInfo(id=scheduler_id,
                                      command=command,
                                      until=until)
        sched_context = SchedulerContext(info=app_timer_info)
        message = f"Until {until}"
        if interval is None:
            interval = datetime.timedelta(minutes=10)
        entry = scheduler.Periodic(callback,
                                   time=round_to_next_second(datetime.datetime.now()),
                                   interval=interval,
                                   context=sched_context)
        await self._scheduler.add(entry)
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        message)

    async def _command_at(self, context: CommandContext,
                          args: ScheduleAtArgs) -> None:
        time, command = args

        async def callback() -> None:
            await self._activate_timer(entry)
        scheduler_id = self._next_scheduler_id()
        app_timer_info = AppTimerInfo(id=scheduler_id,
                                      command=command,
                                      until=None)
        sched_context = SchedulerContext(info=app_timer_info)
        message = f"Scheduled \"{' '.join(command)}\" at {time} (id {scheduler_id})"
        entry: scheduler.Entry[SchedulerContext] = \
            scheduler.OneShot(callback,
                              time=time,
                              context=sched_context)
        await self._scheduler.add(entry)
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        message)

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
            charge_rate         = data["charge_state"]["charge_rate"]
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
            inside_temp         = data["climate_state"]["inside_temp"]
            outside_temp        = data["climate_state"]["outside_temp"]
            message = f"{display_name} version {car_version}\n"
            message += f"Inside: {inside_temp}°{temp_unit} Outside: {outside_temp}°{temp_unit}\n"
            message += f"Heading: {heading} " + self.format_location(Location(lat=lat, lon=lon)) + f" Speed: {speed}\n"
            message += f"Battery: {battery_level}% {battery_range} {dist_unit} est. {est_battery_range} {dist_unit}\n"
            charge_eta = datetime.datetime.now() + datetime.timedelta(hours=time_to_full_charge)
            message += f"Charge limit: {charge_limit}% ";
            if charge_rate:
                message += f" Charge rate: {charge_rate}A";
            if time_to_full_charge > 0 and charge_rate > 0:
                message += f" Ready at: {format_time(charge_eta)} (+{format_hours(time_to_full_charge)})"
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
                await self.control.send_message(context.to_message_context(), f"Success! {result}")
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
