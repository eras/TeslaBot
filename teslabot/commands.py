import re
import logging
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import List, Callable, Coroutine, Any, TypeVar, Generic, Optional, Tuple, Mapping, Union, Awaitable
from .parser import Parser, ParseResult, ParseOK, ParseFail

from . import log

logger = log.getLogger(__name__)
logger.setLevel(logging.DEBUG)

Context = TypeVar("Context")
T = TypeVar("T")
Parsed = TypeVar("Parsed")

class CommandsException(Exception):
    pass

class ParseError(CommandsException):
    pass

class InvocationParseError(ParseError):
    pass

@dataclass
class MarkedWord:
    word: str
    marked: bool

def mark_words(args: List[str], processed: int) -> List[MarkedWord]:
    return [MarkedWord(word=word, marked=idx == processed) for idx, word in enumerate(args)]

class CommandParseError(ParseError):
    marked_args: List[MarkedWord]

    def __init__(self, message: str, marked_args: List[MarkedWord]) -> None:
        super().__init__(message)
        self.marked_args = marked_args

@dataclass
class Invocation:
    name: str
    args: List[str]

    @staticmethod
    def parse(message: str) -> "Invocation":
        fields = [field for field in re.split(r"  *", message) if field != ""]
        if len(fields):
            logger.debug(f"Command: {fields}")
            return Invocation(name=fields[0],
                              args=fields[1:])
        else:
            raise InvocationParseError()

class Command(ABC, Generic[Context]):
    name: str
    description: str

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    @abstractmethod
    async def invoke(self, context: Context, invocation: Invocation) -> None:
        """Run the command"""
        pass

    @abstractmethod
    def parse(self, context: Context, invocation: Invocation) -> bool:
        """Validate the command arguments without invoking it"""
        pass

class Function(Command[Context], Generic[Context, Parsed]):
    parser: Parser[Parsed]
    fn: List[Callable[[Context, Parsed], Coroutine[Any, Any, None]]]

    def __init__(self, name: str, description: str,
                 parser: Parser[Parsed],
                 fn: Callable[[Context, Parsed], Coroutine[Any, Any, None]]) -> None:
        super().__init__(name, description)
        self.parser = parser
        self.fn = [fn]

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        validated = self.parser(invocation.args)
        if isinstance(validated, ParseFail):
            marked_args = [MarkedWord(word=invocation.name, marked=False)]
            marked_args.extend(mark_words(invocation.args,
                                          validated.processed))
            raise CommandParseError(validated.message,
                                    marked_args=marked_args)
        assert isinstance(validated, ParseOK)
        await self.fn[0](context, validated.value)

    def parse(self, context: Context, invocation: Invocation) -> bool:
        return self.parser(invocation.args) is not None

class CommandsParser(Generic[Context], Parser[Callable[[Context], Awaitable[None]]]):
    commands: "Commands[Context]"

    def __init__(self, commands: "Commands[Context]") -> None:
        self.commands = commands

    def parse(self, args: List[str]) -> ParseResult[Callable[[Context], Awaitable[None]]]:
        if len(args) == 0:
            return ParseFail("No command name", processed=0)
        invocation = Invocation(args[0], args[1:])
        if self.commands.has_command(invocation.name):
            async def invoke(context: Context) -> None:
                await self.commands.invoke(context, invocation)
            return ParseOK(invoke, processed=len(args))
        else:
            return ParseFail(f"No such command: {invocation.name}", processed=0)

class Commands(Generic[Context]):
    _commands: List[Command[Context]]

    def __init__(self) -> None:
        self._commands = []

    def register(self, command: Command[Context]) -> None:
        self._commands.append(command)

    def has_command(self, name: str) -> bool:
        return bool([command for command in self._commands if command.name.lower() == name.lower()])

    def parse(self, context: Context, invocation: Invocation) -> bool:
        """Validate requires the matching command to exist"""
        assert self.has_command(invocation.name)
        return bool([command for command in self._commands if command.name == invocation.name])

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        for command in self._commands:
            if command.name.lower() == invocation.name.lower():
                await command.invoke(context, invocation)

    def help(self) -> str:
        results: List[str] = []
        for command in self._commands:
            results.append(f"{command.name}: {command.description}")
        return "\n".join(results)

    def parser(self) -> CommandsParser[Context]:
        return CommandsParser(self)
