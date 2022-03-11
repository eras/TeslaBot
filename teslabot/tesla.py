import asyncio
from typing import List

import teslapy
from urllib.error import HTTPError

from .control import Control, ControlCallback, CommandContext, MessageContext
from .commands import Invocation
from . import log
from .config import Config
from . import commands

logger = log.getLogger(__name__)
logger.setLevel(log.DEBUG)

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

    async def command_callback(self,
                               command_context: CommandContext,
                               invocation: Invocation) -> None:
        """ControlCallback"""
        logger.debug(f"command_callback({invocation.name} {invocation.args})")
        if self._commands.has_command(invocation.name):
            await self._commands.invoke(command_context, invocation)
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

    async def _command_climate(self, context: CommandContext, args: List[str]) -> None:
        if len(args) != 1 and len(args) != 2:
            await self.control.send_message(context.to_message_context(), "usage: !climate on|off [Cherry]")
        else:
            mode = args[0] == "on"
            vehicles = self.tesla.vehicle_list()
            if len(args) == 2:
                display_name = args[1]
                vehicles = [vehicle for vehicle in vehicles if vehicle["display_name"] == display_name]
            if len(vehicles) > 1:
                await self.control.send_message(context.to_message_context(), "Matched more than one vehicle; aborting")
            elif len(vehicles) == 0:
                await self.control.send_message(context.to_message_context(), f"No vehicle found")
            else:
                vehicle = vehicles[0]
                logger.debug(f"vehicle={vehicle}")
                await self.control.send_message(context.to_message_context(), f"Waking up {vehicle['display_name']}")
                try:
                    vehicle.sync_wake_up()
                except teslapy.VehicleError as exn:
                    await self.control.send_message(context.to_message_context(), f"Failed to wake up vehicle; aborting")
                    return
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
