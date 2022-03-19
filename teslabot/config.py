import argparse
from configparser import ConfigParser
from typing import cast, List
from abc import ABC, abstractmethod
from dataclasses import dataclass

class ConfigFileNotFoundError(Exception):
    pass

@dataclass
class Args:
    config: str

def get_args() -> Args:
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('config', type=str,
                        default="teslabot.ini",
                        help='Configuration file name')
    return cast(Args, parser.parse_args())

class Config:
    filename: str
    config: ConfigParser

    def __init__(self,
                 filename: str) -> None:
        self.filename = filename
        self.config = ConfigParser()
        if not self.config.read(filename):
            raise ConfigFileNotFoundError(f"Cannot open config file {filename}")
