import uuid
import re
import logging
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Tuple

from . import commands
from . import parser
from . import log

logger = log.getLogger(__name__)

@dataclass
class CommandContext:
    admin_room: bool
    control: "Control"
    txn: str = field(default_factory=lambda: f"txn {str(uuid.uuid4())}")
    def to_message_context(self) -> "MessageContext":
        return MessageContext(admin_room=self.admin_room)

@dataclass
class MessageContext:
    admin_room: bool

class ControlException(Exception):
    pass

class MessageSendError(ControlException):
    pass

class ConfigError(ControlException):
    pass

class ControlCallback(ABC):
    @abstractmethod
    async def command_callback(self,
                               command_context: CommandContext,
                               invocation: commands.Invocation) -> None:
        """Called when a bot command is received"""

class DefaultControlCallback(ControlCallback):
    async def command_callback(self,
                               command_context: CommandContext,
                               invocation: commands.Invocation) -> None:
        print(f"command_callback({command_context}, {invocation.name} {invocation.args})")

class Control(ABC):
    callback: ControlCallback
    local_commands: commands.Commands[CommandContext]
    require_bang: bool

    def __init__(self) -> None:
        self.callback = DefaultControlCallback()
        self.local_commands = commands.Commands()
        self.local_commands.register(commands.Function("ping", "Ping the bot",
                                                       parser.Empty(), self._command_ping))
        self.require_bang = True

    async def _command_ping(self, context: CommandContext, valid: Tuple[()]) -> None:
        await self.send_message(context.to_message_context(), "pong")

    @abstractmethod
    async def setup(self) -> None:
        """Before calling this, configure the .callback field"""
        pass

    @abstractmethod
    async def send_message(self,
                           message_context: MessageContext,
                           message: str) -> None:
        """Sends a message to the admin or the control channel"""
        pass

    async def process_message(self, command_context: CommandContext, message: str) -> None:
        has_bang = bool(re.match(r"^!", message))
        if not self.require_bang or has_bang:
            logger.info(f"< {message}")
            try:
                try:
                    invocation = commands.Invocation.parse(message[1:] if has_bang else message)
                    if self.local_commands.has_command(invocation.name):
                        await self.local_commands.invoke(command_context, invocation)
                    else:
                        await self.callback.command_callback(command_context, invocation)
                except commands.CommandParseError as exn:
                    logger.error(f"{command_context.txn}: Failed to parse command: {message}")
                    def format(word: str, highlight: bool) -> str:
                        if highlight:
                            return f"_{word}_"
                        else:
                            return word
                    marked = [format(mw.word, mw.marked) for mw in exn.marked_args]
                    await self.send_message(command_context.to_message_context(),
                                            f"{command_context.txn}\n{exn.args[0]}\n{' '.join(marked)}")
                except commands.ParseError as exn:
                    logger.error(f"{command_context.txn}: Failed to parse command: {message}")
                    await self.send_message(command_context.to_message_context(),
                                            f"{command_context.txn}\n{exn}")
            except Exception:
                logger.fatal(f"{command_context.txn}: Failure processing callback: {traceback.format_exc()}")
                await self.send_message(command_context.to_message_context(),
                                        f"{command_context.txn}: Failed to process request")

    @abstractmethod
    async def run(self) -> None:
        """Run indefinitely"""
        pass
