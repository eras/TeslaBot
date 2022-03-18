import asyncio
from typing import Callable, TypeVar, Generic, Union
from dataclasses import dataclass
import concurrent.futures
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
    with concurrent.futures.ThreadPoolExecutor() as pool:
        value_or_exn = await loop.run_in_executor(pool, call_it)
    if isinstance(value_or_exn, Value):
        return value_or_exn.value
    else:
        assert isinstance(value_or_exn, Exception)
        raise value_or_exn
