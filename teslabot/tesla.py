from typing import List

import teslapy

from .control import Control, ControlCallback
from .commands import Invocation
from . import log
from .config import Config
from . import commands

logger = log.getLogger(__name__)
logger.setLevel(log.DEBUG)

class App(ControlCallback):
    control: Control
    config: Config
    _commands: commands.Commands[None]

    def __init__(self, control: Control, config: Config) -> None:
        self.control = control
        self.config = config
        control.callback = self
        self.tesla = teslapy.Tesla(self.config.config["tesla"]["email"])
        self._commands = commands.Commands()
        self._commands.register(commands.Function("authorize", self._command_authorized))
        self._commands.register(commands.Function("vehicles", self._command_vehicles))

    async def command_callback(self, invocation: Invocation) -> None:
        """ControlCallback"""
        logger.debug(f"command_callback({invocation.name} {invocation.args})")
        if self._commands.has_command(invocation.name):
            await self._commands.invoke(None, invocation)
        else:
            await self.control.send_message("No such command")

    async def _command_authorized(self, context: None, args: List[str]) -> None:
        if len(args) != 1:
            await self.control.send_message("usage: !authorize https://the/url/you/ended/up/at")
        else:
            await self.control.send_message("Authorization successful")
            self.tesla.fetch_token(authorization_response=args[0])
            vehicles = self.tesla.vehicle_list()
            await self.control.send_message(str(vehicles[0]))

    async def _command_vehicles(self, context: None, args: List[str]) -> None:
        if len(args) != 0:
            await self.control.send_message("usage: !vehicles")
        else:
            vehicles = self.tesla.vehicle_list()
            await self.control.send_message(f"vehicles: {vehicles}")

    async def run(self) -> None:
        await self.control.send_message("TeslaBot started")
        if not self.tesla.authorized:
            await self.control.send_message(f"Not authorized. Authorization URL: {self.tesla.authorization_url()} \"Page Not Found\" will be shown at success. Use !authorize https://the/url/you/ended/up/at")
