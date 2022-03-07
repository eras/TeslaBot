from typing import Optional, TypeVar

T = TypeVar("T")

def get_optional(x: Optional[T], default: T) -> T:
    if x is None:
        return default
    else:
        return x
