import os
from typing import List, Tuple, Dict
from configparser import ConfigParser
from .state import Section, State

class FileSection(Section):
    def __getitem__(self, key: str) -> str:
        assert isinstance(self.state, FileState)
        return self.state._state[self.section][key]

    def __setitem__(self, key: str, value: str) -> None:
        assert isinstance(self.state, FileState)
        self.state._state[self.section][key] = value

    def has_key(self, key: str) -> bool:
        assert isinstance(self.state, FileState)
        return key in self.state._state[self.section]

    def clear(self) -> None:
        assert isinstance(self.state, FileState)
        self.state._state[self.section].clear()

    def items(self) -> List[Tuple[str, str]]:
        assert isinstance(self.state, FileState)
        return list(self.state._state[self.section].items())

class FileState(State):
    filename: str
    _state: ConfigParser

    def __init__(self,
                 filename: str) -> None:
        super().__init__()
        self.filename = filename
        self._state = ConfigParser()
        self._state.read(filename)

    async def save_to_storage(self) -> None:
        tmp_file_name = self.filename + "~"
        with open(tmp_file_name, 'w') as file:
            self._state.write(file)
        os.replace(tmp_file_name, self.filename)

    def has_section(self, section: str) -> bool:
        return section in self._state

    def __getitem__(self, section: str) -> Section:
        return FileSection(self, section)

    def __setitem__(self, section: str, mapping: Dict[str, str]) -> None:
        self._state[section] = mapping
