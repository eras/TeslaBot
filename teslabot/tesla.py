import asyncio
from typing import List, Optional
import re

import teslapy
from urllib.error import HTTPError

from .control import Control, ControlCallback, CommandContext, MessageContext
from .commands import Invocation
from . import log
from .config import Config
from . import commands
from .utils import assert_some

logger = log.getLogger(__name__)
logger.setLevel(log.DEBUG)

class AppException(Exception):
    pass

class ArgException(AppException):
    pass

class VehicleException(AppException):
    pass

class App(ControlCallback):
    control: Control
    config: Config
    _commands: commands.Commands[CommandContext]

    def __init__(self, control: Control, config: Config) -> None:
        self.control = control
        self.config = config
        control.callback = self
        self.tesla = teslapy.Tesla(self.config.config["tesla"]["email"])
        self._commands = commands.Commands()
        self._commands.register(commands.Function("authorize", self._command_authorized))
        self._commands.register(commands.Function("vehicles", self._command_vehicles))
        self._commands.register(commands.Function("climate", self._command_climate))
        self._commands.register(commands.Function("info", self._command_info))

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

    async def _command_authorized(self, context: CommandContext, args: List[str]) -> None:
        if not context.admin_room:
            await self.control.send_message(context.to_message_context(), "Please use the admin room for this command.")
        elif len(args) != 1:
            await self.control.send_message(context.to_message_context(), "usage: !authorize https://the/url/you/ended/up/at")
        else:
            await self.control.send_message(context.to_message_context(), "Authorization successful")
            self.tesla.fetch_token(authorization_response=args[0])
            vehicles = self.tesla.vehicle_list()
            await self.control.send_message(context.to_message_context(), str(vehicles[0]))

    async def _command_vehicles(self, context: CommandContext, args: List[str]) -> None:
        if len(args) != 0:
            await self.control.send_message(context.to_message_context(), "usage: !vehicles")
        else:
            vehicles = self.tesla.vehicle_list()
            await self.control.send_message(context.to_message_context(), f"vehicles: {vehicles}")

    async def _get_vehicle(self, display_name: Optional[str]) -> teslapy.Vehicle:
        vehicles = self.tesla.vehicle_list()
        if display_name is not None:
            vehicles = [vehicle for vehicle in vehicles if vehicle["display_name"] == display_name]
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

    async def _command_info(self, context: CommandContext, args: List[str]) -> None:
        vehicle = await self._get_vehicle(args[0] if len(args) >= 1 else None)
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
            message += f"Battery: {battery_level}% est. {est_battery_range} {dist_unit}\n"
            message += f"Charge limit: {charge_limit}% Charge rate: {charge_rate}A Time to full: {time_to_full_charge}h\n"
            message += f"Odometer: {odometer}"
            await self.control.send_message(context.to_message_context(),
                                            message)
        except HTTPError as exn:
            await self.control.send_message(context.to_message_context(), str(exn))


    async def _command_climate(self, context: CommandContext, args: List[str]) -> None:
        if len(args) != 1 and len(args) != 2:
            await self.control.send_message(context.to_message_context(), "usage: !climate on|off [Cherry]")
        else:
            mode = args[0] == "on"
            vehicle = await self._get_vehicle(args[1] if len(args) >= 2 else None)
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
        await self.control.send_message(MessageContext(admin_room=False), "TeslaBot started")
        if not self.tesla.authorized:
            await self.control.send_message(MessageContext(admin_room=True), f"Not authorized. Authorization URL: {self.tesla.authorization_url()} \"Page Not Found\" will be shown at success. Use !authorize https://the/url/you/ended/up/at")
