from typing import cast, Optional
from dataclasses import dataclass
import argparse

@dataclass
class Args:
    config: str

def get_args() -> Args:
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('config', type=str,
                        default="teslabot.ini",
                        help='Configuration file name')
    return cast(Args, parser.parse_args())
