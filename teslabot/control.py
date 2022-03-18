import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Tuple
from . import commands
from . import parser

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

    def __init__(self) -> None:
        self.callback = DefaultControlCallback()
        self.local_commands = commands.Commands()
        self.local_commands.register(commands.Function("ping", parser.Empty(), self._command_ping))

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

    @abstractmethod
    async def run(self) -> None:
        """Run indefinitely"""
        pass
