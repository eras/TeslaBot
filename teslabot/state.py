from typing import List, Dict, Union, Optional, Tuple
from abc import ABC, abstractmethod

class StateException(Exception):
    pass

class NotFoundError(StateException):
    pass

class Section(ABC):
    state: "State"
    section: str

    def __init__(self, state: "State", section: str) -> None:
        self.state = state
        self.section = section

    @abstractmethod
    def has_key(self, key: str) -> bool:
        ...

    @abstractmethod
    def __getitem__(self, key: str) -> str:
        ...

    @abstractmethod
    def __setitem__(self, key: str, value: str) -> None:
        ...

    @abstractmethod
    def clear(self) -> None:
        ...

    @abstractmethod
    def items(self) -> List[Tuple[str, str]]:
        ...

class StateElement(ABC):
    @abstractmethod
    async def save(self, state: "State") -> None:
        """This method is called to update the state before saving it"""
        ...

class NoFallBack:
    pass

_NOFALLBACK = NoFallBack()

class State(ABC):
    elements: List[StateElement]

    def __init__(self) -> None:
        self.elements = []

    def add_element(self, element: StateElement) -> None:
        self.elements.append(element)

    async def save(self) -> None:
        for element in self.elements:
            await element.save(self)
        await self.save_to_storage()

    @abstractmethod
    async def save_to_storage(self) -> None:
        ...

    @abstractmethod
    def has_section(self, section: str) -> bool:
        ...

    @abstractmethod
    def __getitem__(self, section: str) -> Section:
        ...

    @abstractmethod
    def __setitem__(self, section: str, mapping: Dict[str, str]) -> None:
        ...

    def get(self, section: str, key: str, fallback: Union[Optional[str], NoFallBack] = _NOFALLBACK) -> Optional[str]:
        if self.has_section(section):
            sect = self[section]
            if sect.has_key(key):
                return sect[key]
        if fallback is _NOFALLBACK:
            raise NotFoundError(f"No state {section}.{key} found")
        elif fallback is None:
            return None
        else:
            assert isinstance(fallback, str)
            return fallback
