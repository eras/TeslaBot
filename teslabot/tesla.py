import asyncio
from typing import List, Optional, Tuple
import re
import datetime

import teslapy
from urllib.error import HTTPError

from .control import Control, ControlCallback, CommandContext, MessageContext
from .commands import Invocation
from . import log
from .config import Config
from . import commands
from .utils import assert_some
from . import scheduler

logger = log.getLogger(__name__)
logger.setLevel(log.DEBUG)

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

class App(ControlCallback):
    control: Control
    config: Config
    tesla: teslapy.Tesla
    _commands: commands.Commands[CommandContext]
    _scheduler: scheduler.Scheduler

    def __init__(self, control: Control, env: Env) -> None:
        self.control = control
        self.config = env.config
        control.callback = self
        self._scheduler = scheduler.Scheduler()
        self.tesla = teslapy.Tesla(self.config.config["tesla"]["email"])
        c = commands
        self._commands = c.Commands()
        valid_climate = c.VldAdjacent(c.VldBool(), c.VldValidOrMissing(ValidVehicle(self.tesla)))
        self._commands.register(c.Function("authorize", c.VldAnyStr(), self._command_authorized))
        self._commands.register(c.Function("vehicles", c.VldEmpty(), self._command_vehicles))
        self._commands.register(c.Function("climate", valid_climate, self._command_climate))
        self._commands.register(c.Function("info", c.VldValidOrMissing(ValidVehicle(self.tesla)), self._command_info))
        self._commands.register(c.Function("at", c.VldAdjacent(c.VldHourMinute(),
                                                               c.VldAdjacent(c.VldFixedStr("climate"),
                                                                             valid_climate)), self._command_at))

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
                                                f"Exception: {exn}")
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

    async def _command_at(self, context: CommandContext,
                          args: Tuple[Tuple[int, int],
                                      Tuple[str, Tuple[bool, Optional[str]]]]) -> None:
        hhmm, command = args
        climate, climate_args = command

        async def callback() -> None:
            logger.info("Timer activated")
            logger.debug(f"now={datetime.datetime.now()}, requested={time}")
            await self._scheduler.remove(entry)
            await self.control.send_message(MessageContext(admin_room=False),
                                            f"Timer activated")
            await self._command_climate(context, climate_args)
        time_of_day = datetime.time(hhmm[0], hhmm[1])
        date = datetime.date.today()
        time = datetime.datetime.combine(date, time_of_day)
        while time < datetime.datetime.now():
            time += datetime.timedelta(days=1)
        entry = scheduler.OneShot(callback, time)
        await self._scheduler.add(entry)
        await self.control.send_message(MessageContext(admin_room=False),
                                        f"Scheduled at {time}")

    async def _command_info(self, context: CommandContext, vehicle_name: Optional[str]) -> None:
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


    async def _command_climate(self, context: CommandContext, args: Tuple[bool, Optional[str]]) -> None:
        mode, vehicle_name = args
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
        await self.control.send_message(MessageContext(admin_room=False), "TeslaBot started")
        if not self.tesla.authorized:
            await self.control.send_message(MessageContext(admin_room=True), f"Not authorized. Authorization URL: {self.tesla.authorization_url()} \"Page Not Found\" will be shown at success. Use !authorize https://the/url/you/ended/up/at")
