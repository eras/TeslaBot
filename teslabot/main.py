import asyncio

from . import log
from . import control
from . import config
from . import state
from .env import Env
from . import tesla
from . import scheduler

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

    scheduler. logger.setLevel(log.INFO)
    tesla.     logger.setLevel(log.DEBUG)
    control.   logger.setLevel(log.INFO)

    args         = config.get_args()
    config_      = config.Config(filename=args.config)
    state_       = state.State(filename=config_.config["common"]["state_file"])
    control_name = config_.config["common"]["control"]
    env          = Env(config=config_,
                       state=state_)
    if control_name == "matrix":
        from . import matrix
        control_ : control.Control = matrix.MatrixControl(env)
        matrix.logger.setLevel(log.INFO)
    elif control_name == "slack":
        from . import slack
        control_ = slack.SlackControl(env=env)
        slack.logger.setLevel(log.DEBUG)
    else:
        logger.fatal(f"Invalid control {control_name}, expected matrix or slack")
        return
    app = tesla.App(env=env, control=control_)
    await control_.setup()
    asyncio.create_task(control_.run())
    asyncio.create_task(app.run())
    while True:
        await asyncio.sleep(3600)

def main() -> None:
    asyncio.get_event_loop().run_until_complete(async_main())
