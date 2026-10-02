import asyncio
from typing import Callable, TypeVar, Generic, Union
from dataclasses import dataclass
T = TypeVar('T', covariant=True)

@dataclass
class Value(Generic[T]):
    """Used so that exceptions and values, that can also be exceptions,
    can be differentiated from each other with instanceof"""
    value: T

async def to_async(fn: Callable[[], T]) -> T:
    def call_it() -> Union[Value[T], Exception]:
        try:
            return Value(fn())
        except Exception as exn:
            return exn
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(None, call_it)
    try:
        value_or_exn = await asyncio.shield(future)
    except asyncio.CancelledError:
        # A sent request cannot be undone. Drain it without blocking the loop
        # before allowing the owner's operation lock to be released.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
        raise
    if isinstance(value_or_exn, Value):
        return value_or_exn.value
    else:
        assert isinstance(value_or_exn, Exception)
        raise value_or_exn
