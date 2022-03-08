from .control import Control, ControlCallback
from .commands import Invocation
from . import log

logger = log.getLogger(__name__)
logger.setLevel(log.DEBUG)

class AppControlCallback(ControlCallback):
    app: "App"
    def __init__(self, app: "App") -> None:
        self.app = app

    async def command_callback(self, invocation: Invocation) -> None:
        logger.debug(f"command_callback({invocation.name} {invocation.args})")

class App:
    def __init__(self, control: Control) -> None:
        control.callback = AppControlCallback(self)

    async def run(self) -> None:
        pass
