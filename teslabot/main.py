import asyncio

from . import log
from . import matrix
from . import config

logger = log.getLogger(__name__)

async def async_main() -> None:
    log.setup_logging()
    logger.setLevel(log.INFO)
    logger.info("Starting")
    args = config.get_args()
    config_ = config.Config(filename=args.config)
    control = matrix.MatrixControl(config=config_)
    await control.setup()
    await control.run()

def main() -> None:
    asyncio.get_event_loop().run_until_complete(async_main())
