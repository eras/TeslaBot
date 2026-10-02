import sys
import logging

ERROR    = logging.ERROR
DEBUG    = logging.DEBUG
CRITICAL = logging.CRITICAL
INFO     = logging.INFO

logger = logging.getLogger()

def setup_logging() -> None:
    logging.getLogger("teslabot").setLevel(logging.INFO)
    # Transport/OAuth SDK logs, including errors, can contain envelopes/tokens.
    for name in ("teslapy", "requests_oauthlib", "oauthlib", "slack", "nio", "aiohttp", "urllib3"):
        sdk_logger = logging.getLogger(name)
        sdk_logger.addHandler(logging.NullHandler())
        sdk_logger.propagate = False
    print_format = logging.Formatter('%(asctime)s %(levelname)-8s %(name)-12s %(message)s')
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(print_format)
    logger.addHandler(console)

def getLogger(name: str) -> logging.Logger:
    return logging.getLogger(name)
