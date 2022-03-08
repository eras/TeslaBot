import re
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import List, Callable, Coroutine, Any, TypeVar, Generic

Context = TypeVar("Context")

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
        pass

class Function(Command, Generic[Context]):
    fn: List[Callable[[Context, List[str]], Coroutine[Any, Any, None]]]

    def __init__(self, name: str, fn: Callable[[Context, List[str]], Coroutine[Any, Any, None]]) -> None:
        super().__init__(name)
        self.fn = [fn]

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        await self.fn[0](context, invocation.args)

class Commands(Generic[Context]):
    _commands: List[Command]

    def __init__(self) -> None:
        self._commands = []

    def register(self, command: Command) -> None:
        self._commands.append(command)

    def has_command(self, name: str) -> bool:
        return bool([command for command in self._commands if command.name == name])

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        for command in self._commands:
            if command.name == invocation.name:
                await command.invoke(context, invocation)
