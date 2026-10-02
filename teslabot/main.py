import asyncio
import os

from . import log
from . import control
from . import config
from . import filestate
from .env import Env
from . import tesla
from . import scheduler
from . import __version__
from typing import Dict, Union
from google.cloud import firestore # type: ignore
from .plugin_exception import PluginException

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

    scheduler. logger.setLevel(log.INFO)
    tesla.     logger.setLevel(log.DEBUG)
    control.   logger.setLevel(log.INFO)

    logger.info(f"Version: {__version__}")

    args         = config.get_args()
    if args.version:
        # We're done here, we just showed version
        return

    logger.info("Starting")
    try:
        secrets: Union[Dict[str, Dict[str, str]], None] = None
        try:
            from importlib import metadata # type: ignore
            if os.getenv("ENVIRONMENT") == "gcp":
                for ep in metadata.entry_points()['secret_sources']:
                    if ep.name == 'gcp':
                        secrets = ep.load()()
        except ImportError as exn:
            logger.warn(f"Cannot import metadata: python 3.8 required; skipping gcp support")
        except PluginException as exn:
            logger.fatal(f"Configuration error: {exn.args[0]}")
            raise SystemExit(1)

        config_      = config.Config(filename=args.config,
                                    config_dict=secrets)
        control_names = control.parse_controls(config_.get("common", "control", fallback="slack"))
        logger.info("Selected controls: %s", ",".join(control_names))
        _db: firestore.CollectionReference = None
        storage = config_.get("common", "storage")
        if storage == "firestore":
            _db = firestore.Client().collection(u"tesla")
            logger.info("Storage in firestore")
        else:
            logger.info("Local storage")
        state_       = filestate.FileState(
                            filename=config_.get("common", "state_file", fallback="state.ini"),
                            _db = _db)
        env          = Env(config=config_,
                            state=state_)
        children = []
        control_ = None
        app = None
        tasks = []
        try:
            for name in control_names:
                if name == "matrix":
                    from .matrix import MatrixControl
                    children.append(MatrixControl(env))
                elif name == "slack":
                    from .slack import SlackControl
                    children.append(SlackControl(env))
                else:
                    from .mqtt import MqttControl
                    children.append(MqttControl(env))
            control_ = control.MultiControl(children)
            app = tesla.App(env=env, control=control_)
            for child in children:
                if not child.run_scheduled_commands:
                    child.set_app(app)
            await app.initialize()
            if not control_.run_scheduled_commands and not app.authorized:
                raise control.ConfigError("MQTT-only startup requires cached Tesla authorization; authorize using a chat adapter first")
            tasks = [asyncio.create_task(control_.run()), asyncio.create_task(app.run())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            raise control.ControlException("Application task returned unexpectedly")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                # Also covers clients constructed before a later constructor failed.
                if control_ is None:
                    control_ = control.MultiControl(children)
                await control_.close()
            finally:
                if app is not None:
                    await app.close()
    except (config.ConfigException, control.ConfigError) as exn:
        logger.fatal("Configuration error: %s", exn.args[0])
        raise SystemExit(1)
    except Exception as exn:
        logger.fatal("Terminal application failure: %s", type(exn).__name__)
        raise SystemExit(1) from None

def main() -> None:
    asyncio.run(async_main())
