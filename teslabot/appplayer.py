import subprocess
from typing import List, Tuple, Optional, Any, Callable, Awaitable, TypeVar, Generic
from dataclasses import dataclass

from . import config
from . import log
from . import parser as p
from . import commands as c
from . import appstorage as appst
from .control import Control, CommandContext

logger = log.getLogger(__name__)

ItemId = str

class AppPlayer:
    control: Control
    _commands: Optional[c.Commands[CommandContext]]
    directory: str
    appstorage: appst.AppStorage
    player: Optional["subprocess.Popen[bytes]"]
    player_executable: str

    def __init__(self,
                 config: config.Config,
                 control: Control,
                 appstorage: appst.AppStorage) -> None:
        self.config = config
        self.control = control
        self.appstorage = appstorage
        self._commands = None
        self.player = None
        self.player_executable = self.config.get("player", "executable")

    def register(self, commands: c.Commands[CommandContext]) -> None:
        # TODO: flow commands from this function to the callbacks (via context?), so that .invoke works
        assert self._commands is None
        self._commands = commands

        commands.register(c.Function("play", "Play an item",
                                     self.appstorage.valid_item(), self._command_play))
        commands.register(c.Function("stop", "Stop playing",
                                     p.Empty(), self._command_stop))

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _stop_playing(self) -> bool:
        if self.player:
            self.player.kill()
            self.player.wait()
            self.player = None
            return True
        else:
            return False

    async def _command_play(self, context: CommandContext, item: appst.ItemId) -> None:
        await self._stop_playing()
        await self.control.send_message(context.to_message_context(),
                                        f"Playing {item}")
        self.player = subprocess.Popen(args=[self.player_executable,
                                             "--",
                                             self.appstorage.item_file(item)],
                                       stdin=subprocess.DEVNULL)

    async def _command_stop(self, context: CommandContext, item: Tuple[()]) -> None:
        if await self._stop_playing():
            await self.control.send_message(context.to_message_context(),
                                            f"Stopped playing")
        else:
            await self.control.send_message(context.to_message_context(),
                                            f"Nothing playing")
