import re
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import List, Callable, Coroutine, Any, TypeVar, Generic, Optional, Tuple, Mapping, Union
from .parser import Parser, ParseOK

Context = TypeVar("Context")
T = TypeVar("T")
Parsed = TypeVar("Parsed")

class InvocationParseError(Exception):
    pass

@dataclass
class Invocation:
    name: str
    args: List[str]

    @staticmethod
    def parse(message: str) -> "Invocation":
        fields = re.split(r"  *", message)
        if len(fields):
            return Invocation(name=fields[0],
                              args=fields[1:])
        else:
            raise InvocationParseError()

class Command(ABC, Generic[Context]):
    name: str

    def __init__(self, name: str) -> None:
        self.name = name

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

    def __init__(self, name: str,
                 parser: Parser[Parsed],
                 fn: Callable[[Context, Parsed], Coroutine[Any, Any, None]]) -> None:
        super().__init__(name)
        self.parser = parser
        self.fn = [fn]

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        validated = self.parser(invocation.args)
        assert isinstance(validated, ParseOK), f"Expected invocation {invocation} to be validated: {validated}"
        await self.fn[0](context, validated.value)

    def parse(self, context: Context, invocation: Invocation) -> bool:
        return self.parser(invocation.args) is not None

class Commands(Generic[Context]):
    _commands: List[Command[Context]]

    def __init__(self) -> None:
        self._commands = []

    def register(self, command: Command[Context]) -> None:
        self._commands.append(command)

    def has_command(self, name: str) -> bool:
        return bool([command for command in self._commands if command.name == name])

    def parse(self, context: Context, invocation: Invocation) -> bool:
        """Validate requires the matching command to exist"""
        assert self.has_command(invocation.name)
        return bool([command for command in self._commands if command.name == invocation.name])

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        for command in self._commands:
            if command.name == invocation.name:
                await command.invoke(context, invocation)
