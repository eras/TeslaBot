import uuid
import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Tuple, Optional, List

from . import commands
from . import parser
from . import log

logger = log.getLogger(__name__)

@dataclass
class CommandContext:
    admin_room: bool
    control: "Control"
    txn: str = field(default_factory=lambda: f"txn {str(uuid.uuid4())}")
    scheduled: bool = False
    def to_message_context(self) -> "MessageContext":
        return MessageContext(admin_room=self.admin_room, origin=None if self.scheduled else self.control)

@dataclass
class MessageContext:
    admin_room: bool
    origin: Optional["Control"] = None

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
        logger.warning("No application callback installed: %s, %s %s", command_context, invocation.name, invocation.args)

class Control(ABC):
    run_scheduled_commands = True
    message_timeout: Optional[float] = 10.0
    local_commands: commands.Commands[CommandContext]

    @property
    def callback(self) -> ControlCallback:
        return self._callback

    @callback.setter
    def callback(self, value: ControlCallback) -> None:
        self._callback = value

    @property
    def require_bang(self) -> bool:
        return self._require_bang

    @require_bang.setter
    def require_bang(self, value: bool) -> None:
        self._require_bang = value

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
            logged_message = re.sub(r"^(!?authorize)\s+.*$", r"\1 [redacted]", message)
            logger.info("< %s", logged_message)
            try:
                try:
                    invocation = commands.Invocation.parse(message[1:] if has_bang else message)
                    if self.local_commands.has_command(invocation.name):
                        await self.local_commands.invoke(command_context, invocation)
                    else:
                        await self.callback.command_callback(command_context, invocation)
                except commands.InvocationEmptyError as exn:
                    logger.debug("Ignoring empty message (or completely commented)")
                except commands.CommandParseError as exn:
                    logger.exception("%s: Failed to parse command: %s", command_context.txn, message)
                    def format(word: str, highlight: bool) -> str:
                        if highlight:
                            return f"_{word}_"
                        else:
                            return word
                    marked = [format(mw.word, mw.marked) for mw in exn.marked_args]
                    await self.send_message(command_context.to_message_context(),
                                            f"{command_context.txn}\n{exn.args[0]}\n{' '.join(marked)}")
                except commands.ParseError as exn:
                    logger.exception("%s: Failed to parse command: %s", command_context.txn, message)
                    await self.send_message(command_context.to_message_context(),
                                            f"{command_context.txn}\n{exn}")
            except (MessageSendError, asyncio.TimeoutError):
                logger.warning("%s: Command response dropped: %s", command_context.txn, type(self).__name__, exc_info=True)
            except Exception as exn:
                logger.exception("%s: Command callback failed: %s", command_context.txn, message)
                raise

    @abstractmethod
    async def run(self) -> None:
        """Run indefinitely"""
        pass

    async def close(self) -> None:
        """Release any adapter-owned sessions after run has stopped."""


def parse_controls(value: str) -> List[str]:
    names = [part.strip() for part in value.split(",")]
    if any(name not in ("matrix", "slack", "mqtt") for name in names) or len(set(names)) != len(names):
        raise ConfigError("common.control requires unique lowercase matrix, slack, mqtt names")
    return names


class MultiControl(Control):
    """Route replies, share chat settings, and own adapter lifetimes."""
    def __init__(self, children: List[Control]) -> None:
        self.children = children
        super().__init__()
        self.run_scheduled_commands = any(child.run_scheduled_commands for child in children)

    @property
    def callback(self) -> ControlCallback:
        return self._callback

    @callback.setter
    def callback(self, value: ControlCallback) -> None:
        self._callback = value
        for child in self.children:
            child.callback = value

    @property
    def require_bang(self) -> bool:
        return self._require_bang

    @require_bang.setter
    def require_bang(self, value: bool) -> None:
        self._require_bang = value
        for child in self.children:
            child.require_bang = value

    async def setup(self) -> None:
        # Each adapter initializes independently inside its supervised run task.
        pass

    async def send_message(self, message_context: MessageContext, message: str) -> None:
        if message_context.origin is not None:
            if message_context.origin not in self.children or not message_context.origin.run_scheduled_commands:
                raise MessageSendError("Unknown chat origin")
            await asyncio.wait_for(message_context.origin.send_message(message_context, message),
                                   message_context.origin.message_timeout)
            return
        async def send(child: Control) -> None:
            try:
                await asyncio.wait_for(child.send_message(message_context, message), child.message_timeout)
            except Exception:
                logger.warning("Chat notification dropped: %s, context %s, message %s", type(child).__name__, message_context, message, exc_info=True)
        await asyncio.gather(*(send(child) for child in self.children if child.run_scheduled_commands))

    async def run(self) -> None:
        async def serve(child: Control) -> None:
            try:
                await child.setup()
                logger.info("Adapter initialized: %s", type(child).__name__)
                await child.run()
            except Exception as exn:
                logger.exception("Adapter failed: %s", type(child).__name__)
                raise
            logger.error("Adapter returned unexpectedly: %s", type(child).__name__)
            raise ControlException("Adapter returned unexpectedly: " + type(child).__name__)
        tasks = [asyncio.create_task(serve(child)) for child in self.children]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        results = await asyncio.gather(*(child.close() for child in self.children), return_exceptions=True)
        for child, result in zip(self.children, results):
            if isinstance(result, BaseException):
                logger.error("Adapter cleanup failed: %s", type(child).__name__, exc_info=(type(result), result, result.__traceback__))
