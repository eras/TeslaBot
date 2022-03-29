import datetime
import json
import time
import os
import re
from typing import List, Tuple, Optional, Any, Callable, Awaitable, TypeVar, Generic
from dataclasses import dataclass

from . import config
from . import log
from . import parser as p
from . import commands as c
from .control import Control, CommandContext

T = TypeVar('T')

logger = log.getLogger(__name__)

ItemId = str

# def cmd_adjacent(label: str, parser: p.Parser[T]) -> p.Parser[Tuple[str, T]]:
#     return p.Labeled(label=label, parser=p.Adjacent(p.CaptureFixedStr(label), parser).base())

class AppStorage:
    control: Control
    _commands: Optional[c.Commands[CommandContext]]
    directory: str

    def __init__(self,
                 config: config.Config,
                 control: Control) -> None:
        self.config = config
        self.directory = config.get("storage", "directory", empty_is_none=True)
        self.control = control
        self._commands = None

    def register(self, commands: c.Commands[CommandContext]) -> None:
        # TODO: flow commands from this function to the callbacks (via context?), so that .invoke works
        assert self._commands is None
        self._commands = commands

        commands.register(c.Function("ls", "List items",
                                     p.Empty(), self._command_ls))
        commands.register(c.Function("rm", "Remove items",
                                     self.valid_item(), self._command_rm))

    def item_file(self, item: ItemId) -> str:
        return f"{self.directory}/{item}"

    def valid_item(self) -> p.Parser[ItemId]:
        def check_exists(item: ItemId) -> Optional[str]:
            if os.path.exists(self.item_file(item)):
                return None
            else:
                return f"{item} does not exist"
        def check_badness(item: ItemId) -> Optional[str]:
            if re.match(r"^[/.]", item):
                return f"Item name cannot start with . or /"
            if re.search(r"/\.", item):
                return f"Item component cannot start with ."
            return None
        return p.Validate(check_exists,
                          p.Validate(check_badness,
                                     p.AnyStr()))

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _command_ls(self, context: CommandContext, valid: Tuple[()]) -> None:
        items = os.listdir(self.directory)
        if items:
            result: List[str] = []
            for item in items:
                # TODO: quoting
                result.append(f"{item}")
            result_lines = "\n".join(result)
            await self.control.send_message(context.to_message_context(), result_lines)
        else:
            await self.control.send_message(context.to_message_context(), "No contents")

    async def _command_rm(self, context: CommandContext,
                          id: ItemId) -> None:
        try:
            os.remove(self.item_file(id))
            await self.control.send_message(context.to_message_context(),
                                            f"Removed")
        except FileNotFoundError:
            await self.control.send_message(context.to_message_context(),
                                            f"No such item")

