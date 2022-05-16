import argparse
from configparser import ConfigParser, SectionProxy
from typing import cast, List, Optional, Union, Dict, Any
from abc import ABC, abstractmethod
from dataclasses import dataclass
import os


class ConfigException(Exception):
    pass

class ConfigFileNotFoundError(ConfigException):
    pass

class ConfigNotFound(ConfigException):
    pass

@dataclass
class Args:
    config: str
    version: bool

def get_args() -> Args:
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('--config', type=str, default="config.ini",
                        help='Configuration file name')
    parser.add_argument('--version', action='store_true', help="Show version")
    return cast(Args, parser.parse_args())

class Section:
    section_name: str
    section: SectionProxy
    def __init__(self, section_name: str, section: SectionProxy):
        self.section_name = section_name
        self.section = section

    def get(self,
            key: str,
            fallback: Optional[str] = None,
            empty_is_none: bool = True) -> str:
        found = key in self.section
        value = self.section[key] if found else fallback
        if value is None or (empty_is_none and value == ""):
            if found:
                raise ConfigNotFound(f"Empty value not permitted for {self.section_name}.{key} in config")
            else:
                raise ConfigNotFound(f"Cannot find {self.section_name}.{key} in config")
        else:
            return value

    def __getitem__(self, key: str) -> str:
        return self.get(key)

class Config:
    filename: str
    _config: ConfigParser

    def __init__(self,
                 filename: str,
                 config_dict: Dict[str, Dict[str, str]]) -> None:
        self.filename = filename
        self._config = ConfigParser()

        # Try first to read config from plugin
        
        if config_dict is not None:
            self._config.read_dict(config_dict)
        else:
            currentPath = os.path.dirname(__file__)
            if not self._config.read(os.path.join(currentPath, filename)):
                raise ConfigFileNotFoundError(f"Cannot open config file {filename}")

    def __getitem__(self, section: str) -> Section:
        return Section(section, self._config[section])

    def has_section(self, key: str) -> bool:
        return self._config.has_section(key)

    def get(self, section: str, key: str,
            fallback: Optional[str] = None,
            empty_is_none: bool = True) -> str:
        return Section(section, self._config[section]).get(key, fallback=fallback, empty_is_none=empty_is_none)
