import asyncio
from typing import List, Optional, Tuple, Callable, Awaitable, Any, TypeVar
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
from .env import Env
from .locations import Location, Locations, LocationArgs, LocationArgsParser, LocationCommandContextBase, LocationInfoCoords, LatLon
from .asyncthread import to_async
from . import __version__
from .appscheduler import AppScheduler
from .appstorage import AppStorage, ItemId

logger = log.getLogger(__name__)

T = TypeVar('T')

class AppException(Exception):
    pass

class ArgException(AppException):
    pass

class AppState(StateElement):
    app: "App"

    def __init__(self, app: "App") -> None:
        self.app = app

    async def save(self, state: State) -> None:
        # TODO: move this to Control
        if not state.has_section("control"):
            state["control"] = {}
        state["control"]["require_bang"] = str(self.app.control.require_bang)

def cmd_adjacent(label: str, parser: p.Parser[T]) -> p.Parser[Tuple[str, T]]:
    return p.Labeled(label=label, parser=p.Adjacent(p.CaptureFixedStr(label), parser).base())

SetArgs = Callable[[CommandContext], Awaitable[None]]
def SetArgsParser(app: "App") -> p.Parser[SetArgs]:
    return app._set_commands.parser()

class App(ControlCallback):
    control: Control
    config: Config
    state: State
    _commands: commands.Commands[CommandContext]
    _set_commands: commands.Commands[CommandContext]
    _scheduler: AppScheduler[CommandContext]
    _storage: AppStorage

    def __init__(self, control: Control, env: Env) -> None:
        self.control = control
        self.config = env.config
        self.state = env.state
        control.callback = self
        c = commands
        self._storage = AppStorage(config=self.config,
                                   control=self.control)
        self._scheduler = AppScheduler(
            state=self.state,
            control=self.control,
            schedulable_commands=[
                cmd_adjacent("play", self._storage.valid_item()).any(),
            ])
        self._commands = c.Commands()
        self._scheduler.register(self._commands)
        self._storage.register(self._commands)
        self._commands.register(c.Function("help", "Show help",
                                           p.Empty(), self._command_help))
        self._commands.register(c.Function("play", "Play an item",
                                           self._storage.valid_item(), self._command_play))

        self._set_commands = c.Commands()
        # TODO: move this to Control
        self._set_commands.register(c.Function("require-!", "true or false, whether to require ! in front of commands",
                                               p.Remaining(p.Bool()), self._command_set_require_bang))

        self._commands.register(c.Function("set", f"Set a configuration parameter\n{indent(2, self._set_commands.help())}",
                                           SetArgsParser(self), self._command_set))

    async def _command_help(self, command_context: CommandContext, args: Tuple[()]) -> None:
        await self.control.send_message(command_context.to_message_context(),
                                        self._commands.help())

    async def _command_play(self, context: CommandContext, item_id: ItemId) -> None:
        pass

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

    # TODO: move this to Control
    async def _command_set_require_bang(self, context: CommandContext, args: bool) -> None:
        self.control.require_bang = args
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        f"Require bang set to {self.control.require_bang}")

    async def _load_state(self) -> None:
        # TODO: move this to Control
        if self.state.has_section("control"):
            self.control.require_bang = bool(self.state.get("control", "require_bang", fallback=str(self.control.require_bang)) == str(True))

    async def run(self) -> None:
        await self._scheduler.start()
        await self._load_state()
        await self.control.send_message(MessageContext(admin_room=False), f"MusicBot {__version__} started")
        self.state.add_element(AppState(self))
