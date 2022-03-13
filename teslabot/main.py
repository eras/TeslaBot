import asyncio

from . import log
from .control import Control
from . import matrix
from . import slack
from . import config
from . import state
from .env import Env
from . import tesla

logger = log.getLogger(__name__)

async def dump_all_tasks() -> None:
    while True:
        for x in asyncio.all_tasks():
            print(x)
        print("----")
        await asyncio.sleep(0.5)

async def async_main() -> None:
    log.setup_logging()
    logger.setLevel(log.INFO)
    logger.info("Starting")
    matrix.logger.setLevel(log.INFO)
    slack.logger.setLevel(log.DEBUG)
    args = config.get_args()
    config_ = config.Config(filename=args.config)
    state_ = state.State(filename=config_.config["common"]["state_file"])
    control_name = config_.config["common"]["control"]
    if control_name == "matrix":
        control : Control = matrix.MatrixControl(Env(config=config_, state=state_))
    elif control_name == "slack":
        control = slack.SlackControl(Env(config=config_, state=state_))
    else:
        logger.fatal(f"Invalid control {control_name}, expected matrix or slack")
        return
    app = tesla.App(config=config_, control=control)
    await control.setup()
    asyncio.create_task(control.run())
    asyncio.create_task(app.run())
    while True:
        await asyncio.sleep(3600)

def main() -> None:
    asyncio.get_event_loop().run_until_complete(async_main())
