from dataclasses import dataclass

from . import config
from . import state

@dataclass
class Env:
    config: config.Config
    state: state.State
