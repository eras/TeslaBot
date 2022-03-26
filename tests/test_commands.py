import asyncio
import unittest
import datetime
from typing import List, TypeVar, Optional, Tuple
from enum import Enum

import teslabot.commands as c
import teslabot.parser as p

Result = TypeVar('Result', bound=object)

class TestCommands(unittest.TestCase):
    def __init__(self, method_name: str) -> None:
        super().__init__(method_name)
        self.longMessage = True

    def setup_commands(self, called: List[Optional[Result]], valid: p.Parser[Result]) -> c.Commands[None]:
        cmds = c.Commands[None]()
        async def command0(context: None, valid: Result) -> None:
            called[0] = valid
        async def command1(context: None, valid: Result) -> None:
            called[1] = valid
        cmds.register(c.Function("test0", "", valid, command0))
        cmds.register(c.Function("test1", "", valid, command1))
        return cmds

    def test_simple_call(self) -> None:
        async def test() -> None:
            called: List[Optional[p.EmptyVal]] = [None, None]
            cmds = self.setup_commands(called, p.Empty())
            await cmds.invoke(None, c.Invocation(name="test0", args=[]))
            self.assertIsNotNone(called[0], "Command test0 was not called")
            self.assertIsNone(called[1], "Command test1 was called")
        asyncio.get_event_loop().run_until_complete(test())

    def test_validated_call(self) -> None:
        async def test() -> None:
            called: List[Optional[str]] = [None, None]
            cmds = self.setup_commands(called, p.AnyStr())
            await cmds.invoke(None, c.Invocation(name="test0", args=["arg1"]))
            self.assertEqual(called[0], "arg1", "Command test0 was not called")
            self.assertIsNone(called[1], "Command test1 was called")
        asyncio.get_event_loop().run_until_complete(test())
