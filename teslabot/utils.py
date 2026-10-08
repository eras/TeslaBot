import asyncio
from configparser import ConfigParser
import datetime
from typing import Optional, TypeVar, Callable, Awaitable, Any

T = TypeVar("T")
U = TypeVar("U")

def get_optional(x: Optional[T], default: T) -> T:
    if x is None:
        return default
    else:
        return x

def assert_some(x: Optional[T], message: Optional[str] = None) -> T:
    if message is not None:
        assert x is not None, message
    else:
        assert x is not None
    return x

def round_to_next_second(time: datetime.datetime) -> datetime.datetime:
    return time + datetime.timedelta(microseconds=1000000-time.microsecond)

def indent(by: int, string: str) -> str:
    prefix = " " * by
    return ''.join([f"{prefix}{st}" for st in string.splitlines(True)])

async def call_with_delay_info(delay_sec: float,
                               report: Callable[[], Awaitable[None]],
                               task: Awaitable[T]) -> T:
    async def delayed() -> None:
        await asyncio.sleep(delay_sec)
        await report()
    report_task = asyncio.create_task(delayed())
    try:
        return await task
    finally:
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)

def coalesce(*xs: Optional[T]) -> T:
    """Return the first non-None value from the list; there must be at least one"""
    for x in xs:
        if x is not None:
            return x
    assert False, "Expected at least one element to be non-None"

def map_optional(x: Optional[T], fn: Callable[[T], U]) -> Optional[U]:
    if x is None:
        return None
    else:
        return fn(x)

# Create json-like dict from ConfigParser data
def parser_to_dict(parser: ConfigParser) -> dict[str, dict[str, Any]]:
    json_dict: dict[str, dict[str, Any]] = {}
    for section in parser.sections():
        json_dict[section] = {}
        for key in parser[section]:
            json_dict[section][key] = parser[section][key]

    return json_dict
