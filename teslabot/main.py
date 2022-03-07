from . import matrix
from . import config

async def async_main():
    args = config.get_args()
    control = matrix.MatrixControl()
    config_ = config.Config(filename=args.config)
    await control.setup()
    await control.run()

def main():
    asyncio.get_event_loop().run_until_complete(async_main())
