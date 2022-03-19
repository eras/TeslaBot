from typing import Optional, TypeVar

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
