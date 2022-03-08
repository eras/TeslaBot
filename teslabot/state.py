from configparser import ConfigParser
from typing import List
from abc import ABC, abstractmethod

class StateElement(ABC):
    @abstractmethod
    def save(self, state: ConfigParser) -> None:
        """This method is called to update the state before saving it"""
        pass

class State:
    filename: str
    state: ConfigParser
    elements: List[StateElement]

    def __init__(self,
                 filename: str) -> None:
        self.filename = filename
        self.state = ConfigParser()
        self.elements = []
        self.state.read(filename)

    def add_element(self, element: StateElement) -> None:
        self.elements.append(element)

    def save(self) -> None:
        for element in self.elements:
            element.save(self.state)
        # TODO: use safe code for overwriting
        with open(self.filename, 'w') as file:
            self.state.write(file)
