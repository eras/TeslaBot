import asyncio
from typing import Optional, TypeVar, Callable, Awaitable, List

T = TypeVar("T")

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

def indent(by: int, string: str) -> str:
    prefix = " " * by
    return ''.join([f"{prefix}{st}" for st in string.splitlines(True)])

async def call_with_delay_info(delay_sec: float,
                               report: Callable[[], Awaitable[None]],
                               task: Awaitable[T]) -> T:
    result: List[T] = []
    exn: List[Exception] = []
    async def delayed() -> None:
        try:
            await asyncio.sleep(delay_sec)
            await report()
        except Exception as exn:
            report_task.cancel()
            raise exn
    async def invoke() -> None:
        try:
            result.append(await task);
            report_task.cancel()
        except Exception as exn2:
            exn.append(exn2)
            report_task.cancel()
    report_task = asyncio.create_task(delayed())
    invoke_task = asyncio.create_task(invoke())
    try:
        await asyncio.gather(invoke_task,
                             report_task)
    except asyncio.CancelledError:
        # risen if invoke cancels delayed
        pass
    if exn:
        raise exn[0]
    else:
        return result[0]

def coalesce(*xs: Optional[T]) -> T:
    """Return the first non-None value from the list; there must be at least one"""
    for x in xs:
        if x is not None:
            return x
    assert False, "Expected at least one element to be non-None"
