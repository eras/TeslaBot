from abc import ABC, abstractmethod

class ControlCallback(ABC):
    @abstractmethod
    async def command_callback(self, message: str) -> None:
        """Called when a bot command is received"""

class DefaultControlCallback(ControlCallback):
    async def command_callback(self, message: str) -> None:
        print(f"command_callback({message})")

class Control(ABC):
    callback: ControlCallback

    def __init__(self) -> None:
        self.callback = DefaultControlCallback()

    @abstractmethod
    async def setup(self) -> None:
        """Before calling this, configure the .callback field"""
        pass

    @abstractmethod
    async def send_message(self, message: str) -> None:
        """Sends a message to the control channel"""
        pass
