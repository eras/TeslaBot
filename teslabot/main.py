import asyncio

from . import matrix
from . import config

async def async_main() -> None:
    args = config.get_args()
    config_ = config.Config(filename=args.config)
    control = matrix.MatrixControl(config=config_)
    await control.setup()
    await control.run()

def main() -> None:
    asyncio.get_event_loop().run_until_complete(async_main())
