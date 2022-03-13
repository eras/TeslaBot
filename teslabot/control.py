from abc import ABC, abstractmethod
from dataclasses import dataclass
from .commands import Invocation

@dataclass
class CommandContext:
    admin_room: bool
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
                               invocation: Invocation) -> None:
        """Called when a bot command is received"""

class DefaultControlCallback(ControlCallback):
    async def command_callback(self,
                               command_context: CommandContext,
                               invocation: Invocation) -> None:
        print(f"command_callback({command_context}, {invocation.name} {invocation.args})")

class Control(ABC):
    callback: ControlCallback

    def __init__(self) -> None:
        self.callback = DefaultControlCallback()

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
