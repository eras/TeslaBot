from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import List

@dataclass
class Invocation:
    name: str
    args: List[str]

class Command(ABC):
    name: str

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def invoke(self, invocation: Invocation) -> None:
        pass

class Commands:
    _commands: List[Command]

    def __init__(self) -> None:
        self._commands = []

    def register(self, command: Command) -> None:
        self._commands.append(command)

    def has_command(self, name: str) -> bool:
        return bool([command for command in self._commands if command.name == name])

    async def invoke(self, invocation: Invocation) -> None:
        for command in self._commands:
            if command.name == invocation.name:
                await command.invoke(invocation)
