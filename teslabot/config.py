import argparse
from configparser import ConfigParser
from typing import cast, List
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Args:
    config: str

def get_args() -> Args:
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('config', type=str,
                        default="teslabot.ini",
                        help='Configuration file name')
    return cast(Args, parser.parse_args())

class ConfigElement(ABC):
    @abstractmethod
    def save(self, config: ConfigParser) -> None:
        """This method is called to update the configuration before saving it"""
        pass

class Config:
    filename: str
    config: ConfigParser
    elements: List[ConfigElement]

    def __init__(self,
                 filename: str) -> None:
        self.filename = filename
        self.config = ConfigParser()
        self.elements = []
        self.config.read(filename)

    def add_element(self, element: ConfigElement) -> None:
        self.elements.append(element)

    def save(self) -> None:
        for element in self.elements:
            element.save(self.config)
        # TODO: use safe code for overwriting
        with open(self.filename, 'w') as file:
            self.config.write(file)
