import asyncio
from typing import List, Optional, Tuple, Generic, Callable, Awaitable, cast, Any, Union
import re
import datetime
from configparser import ConfigParser
from dataclasses import dataclass
import traceback
import json

import teslapy
from urllib.error import HTTPError

from .control import Control, ControlCallback, CommandContext, MessageContext
from .commands import Invocation
from . import log
from .config import Config
from .state import State, StateElement
from . import commands
from .utils import assert_some
from . import scheduler
from .env import Env

logger = log.getLogger(__name__)

class AppException(Exception):
    pass

class ArgException(AppException):
    pass

class VehicleException(AppException):
    pass

class ValidVehicle(commands.VldDelayed[str]):
    tesla: teslapy.Tesla

    def __init__(self, tesla: teslapy.Tesla) -> None:
        super().__init__(self.make_validator)
        self.tesla = tesla

    def make_validator(self) -> commands.Validator[str]:
        vehicles = self.tesla.vehicle_list()
        display_names = [vehicle["display_name"] for vehicle in vehicles]
        return commands.VldOneOfStrings(display_names)

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

ClimateArgs = Tuple[Tuple[bool, Optional[str]], Tuple[()]]
def valid_climate(app: "App") -> commands.Validator[ClimateArgs]:
    return commands.VldAdjacent(commands.VldAdjacent(commands.VldBool(), commands.VldValidOrMissing(ValidVehicle(app.tesla))),
                                commands.VldEmpty())

InfoArgs = Tuple[Optional[str], Tuple[()]]
def valid_info(app: "App") -> commands.Validator[InfoArgs]:
    return commands.VldAdjacent(commands.VldValidOrMissing(ValidVehicle(app.tesla)),
                                commands.VldEmpty())

SchedulableType = List[str]
def valid_schedulable(app: "App") -> commands.Validator[SchedulableType]:
    cmds = [
        commands.VldAdjacent(commands.VldFixedStr("climate"), valid_climate(app)).any(),
        commands.VldAdjacent(commands.VldFixedStr("info"), valid_info(app)).any(),
    ]
    return commands.VldCaptureOnly(commands.VldOneOf(cmds))

class App(ControlCallback):
    control: Control
    config: Config
    state: State
    tesla: teslapy.Tesla
    _commands: commands.Commands[CommandContext]
    _scheduler: scheduler.Scheduler
    _scheduler_id: int

    def __init__(self, control: Control, env: Env) -> None:
        self.control = control
        self.config = env.config
        self.state = env.state
        self.state.add_element(AppState(self))
        control.callback = self
        self._scheduler = scheduler.Scheduler()
        self.tesla = teslapy.Tesla(self.config.config["tesla"]["email"])
        self._scheduler_id = 1
        c = commands
        self._commands = c.Commands()
        self._commands.register(c.Function("authorize", c.VldAnyStr(), self._command_authorized))
        self._commands.register(c.Function("vehicles", c.VldEmpty(), self._command_vehicles))
        self._commands.register(c.Function("climate", valid_climate(self), self._command_climate))
        self._commands.register(c.Function("info", valid_info(self), self._command_info))
        self._commands.register(c.Function("at", c.VldAdjacent(c.VldHourMinute(), valid_schedulable(self)), self._command_at))
        self._commands.register(c.Function("rm", c.VldInt(), self._command_rm))
        self._commands.register(c.Function("ls", c.VldEmpty(), self._command_ls))

    def _next_scheduler_id(self) -> int:
        id = self._scheduler_id
        self._scheduler_id += 1
        return id

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
            except Exception as exn:
                logger.error(str(exn))
                await self.control.send_message(command_context.to_message_context(),
                                                f"Exception: {traceback.format_exc()}")
        else:
            await self.control.send_message(command_context.to_message_context(), "No such command")

    async def _command_authorized(self, context: CommandContext, authorization_response: str) -> None:
        if not context.admin_room:
            await self.control.send_message(context.to_message_context(), "Please use the admin room for this command.")
        else:
            await self.control.send_message(context.to_message_context(), "Authorization successful")
            self.tesla.fetch_token(authorization_response=authorization_response)
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
            vehicle.sync_wake_up()
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
        if not self.state.state.has_section("tesla.timers"):
            return
        for id, timer in self.state.state["tesla.timers"].items():
            self._scheduler_id = max(self._scheduler_id, int(id) + 1)
            entry = AppOneShot.from_json(int(id), json.loads(timer), callback=self._activate_timer)
            await self._scheduler.add(entry)

    async def _activate_timer(self, entry: scheduler.Entry) -> None:
        if isinstance(entry, AppOneShot):
            command = entry.info.command
            logger.info(f"Timer {entry.info.id} activated")
            await self._scheduler.remove(entry)
            await self.state.save()
            context = CommandContext(admin_room=False)
            await self.control.send_message(context.to_message_context(), f"Timer activated: \"{' '.join(command)}\"")
            invocation = commands.Invocation(name=command[0], args=command[1:])
            await self._commands.invoke(context, invocation)
            await self._command_ls(context, ())
        else:
            logger.info(f"Unknown timer {entry} activated..")

    async def _command_at(self, context: CommandContext,
                          args: Tuple[Tuple[int, int], SchedulableType]) -> None:
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

    async def _command_info(self, context: CommandContext, args: InfoArgs) -> None:
        vehicle_name, _ = args
        vehicle = await self._get_vehicle(vehicle_name)
        await self._wake(context, vehicle)
        try:
            data = vehicle.get_vehicle_data()
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
            message = ""
            message += f"Heading: {heading} Lat: {lat} Lon: {lon} Speed: {speed}\n"
            message += f"Inside: {inside_temp}°{temp_unit} Outside: {outside_temp}°{temp_unit}\n"
            message += f"Battery: {battery_level}% {battery_range} {dist_unit} est. {est_battery_range} {dist_unit}\n"
            message += f"Charge limit: {charge_limit}% Charge rate: {charge_rate}A Time to full: {time_to_full_charge}h\n"
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
                result = vehicle.command(command)
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
        await self.control.send_message(MessageContext(admin_room=False), "TeslaBot started")
        if not self.tesla.authorized:
            await self.control.send_message(MessageContext(admin_room=True), f"Not authorized. Authorization URL: {self.tesla.authorization_url()} \"Page Not Found\" will be shown at success. Use !authorize https://the/url/you/ended/up/at")
