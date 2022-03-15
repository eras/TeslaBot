import asyncio
import unittest
from typing import List, TypeVar, Optional, Tuple

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
        cmds.register(c.Function("test0", valid, command0))
        cmds.register(c.Function("test1", valid, command1))
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

    def test_simple_validators(self) -> None:
        with self.subTest():
            self.assertEqual(p.Empty().parse([]),
                             p.ParseOK((), processed=0))
        with self.subTest():
            self.assertEqual(p.Empty().parse(["hei"]),
                             p.ParseFail("Expected no more arguments"))
        with self.subTest():
            self.assertEqual(p.AnyStr().parse([]),
                             p.ParseFail("No argument provided"))
        with self.subTest():
            self.assertEqual(p.AnyStr().parse([""]),
                             p.ParseOK("", processed=1))
        with self.subTest():
            self.assertEqual(p.AnyStr().parse(["moi"]),
                             p.ParseOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(p.AnyStr().parse(["moi", "moi"]),
                             p.ParseOK("moi", processed=1))

    def test_bool(self) -> None:
        with self.subTest():
            self.assertEqual(p.Bool().parse([]),
                             p.ParseFail("No argument provided"))
        with self.subTest():
            self.assertEqual(p.Bool().parse([""]),
                             p.ParseFail('Invalid argument "" for boolean'))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean'))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["True"]),
                             p.ParseOK(True, processed=1))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["true"]),
                             p.ParseOK(True, processed=1))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["FALSE"]),
                             p.ParseOK(False, processed=1))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["false"]),
                             p.ParseOK(False, processed=1))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["0"]),
                             p.ParseOK(False, processed=1))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["1"]),
                             p.ParseOK(True, processed=1))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["off"]),
                             p.ParseOK(False, processed=1))
        with self.subTest(): self.assertEqual(p.Bool().parse(["on"]),
                                              p.ParseOK(True, processed=1))

    def test_regex(self) -> None:
        with self.subTest():
            self.assertEqual(p.Regex(r".*", [0]).parse([""]),
                             p.ParseOK("", processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r".*", [0]).parse(["moi"]),
                             p.ParseOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r".(oi)", [0]).parse(["moi"]),
                             p.ParseOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r".(oi)", [1]).parse(["moi"]),
                             p.ParseOK("oi", processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r".(o)(i)", [1, 2]).parse(["moi"]),
                             p.ParseOK(("o", "i"), processed=1))

    def test_map(self) -> None:
        def negate(x: bool) -> bool:
            return not x
        with self.subTest():
            self.assertEqual(p.Map(p.Bool(), negate).parse([]),
                             p.ParseFail("No argument provided"))
        with self.subTest():
            self.assertEqual(p.Map(p.Bool(), negate).parse(["true"]),
                             p.ParseOK(False, processed=1))
        with self.subTest():
            self.assertEqual(p.Map(p.Bool(), negate).parse(["false"]),
                             p.ParseOK(True, processed=1))

    # def test_one_of(self) -> None:
    #     with self.subTest():
    #         self.assertEqual(p.VldOneOf("").parse([]),
    #                          p.ValidatorFail("No argument provided"))

    def test_optional(self) -> None:
        with self.subTest():
            self.assertEqual(p.Optional_(p.Bool()).parse([]),
                             p.ParseOK(None, processed=0))
        with self.subTest():
            self.assertEqual(p.Optional_(p.Bool()).parse(["moi"]),
                             p.ParseOK(None, processed=0))
        with self.subTest():
            self.assertEqual(p.Optional_(p.Bool()).parse(["true"]),
                             p.ParseOK(True, processed=1))

        with self.subTest():
            self.assertEqual(p.ValidOrMissing(p.Bool()).parse([]),
                             p.ParseOK(None, processed=0))
        with self.subTest():
            self.assertEqual(p.ValidOrMissing(p.Bool()).parse(["moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean'))
        with self.subTest():
            self.assertEqual(p.ValidOrMissing(p.Bool()).parse(["true"]),
                             p.ParseOK(True, processed=1))

    def test_adjacent(self) -> None:
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse([]),
                             p.ParseFail("No argument provided while parsing first argument"))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse(["moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing first argument'))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse(["moi", "1"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing first argument'))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse(["1"]),
                             p.ParseFail("No argument provided while parsing second argument"))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse(["1", "moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing second argument'))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse(["1", "0"]),
                             p.ParseOK((True, False), processed=2))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Adjacent(p.Bool(),
                                                         p.Empty())).parse(["1", "0"]),
                             p.ParseOK((True, (False, ())), processed=2))

    def test_seq(self) -> None:
        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse([]),
                             p.ParseFail("No argument provided while parsing argument 1"))

        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse(["moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing argument 1'))

        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse(["moi", "moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing argument 1'))

        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse(["1", "moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing argument 2'))

        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse(["1", "0"]),
                             p.ParseOK([True, False], processed=2))
        with self.subTest():
            self.assertEqual(p
                             .Seq([p.Seq([p.Map(p.Bool(), str).base(),
                                                p.Map(p.AnyStr(), str)]),
                                      p.Map[bool, List[str]](p.Bool(), lambda x: [str(x)]),
                                      p.Map[p.EmptyVal, List[str]](p.Empty(), lambda _: [])])
                             .parse(["1", "moi", "0"]),
                             p.ParseOK([["True", "moi"], ["False"], []], processed=3))

        # this is probably more practical though less safely typed..
        with self.subTest():
            self.assertEqual(p
                             .Seq([p.Seq([p.Bool().any(),
                                                p.AnyStr().any()]),
                                      p.Bool().any(),
                                      p.Empty().any()])
                             .parse(["1", "moi", "0"]),
                             p.ParseOK([[True, "moi"], False, ()], processed=3))

        with self.subTest():
            self.assertEqual(p.Tag("tag", p.Bool())
                             .parse([]),
                             p.ParseFail("No argument provided"))
        with self.subTest():
            self.assertEqual(p.Tag("tag", p.Bool())
                             .parse(["true"]),
                             p.ParseOK(("tag", True), processed=1))
        with self.subTest():
            self.assertEqual(p.Seq([p.Tag("tag1", p.Bool()),
                                       p.Tag("tag2", p.Bool())])
                             .parse(["true", "false"]),
                             p.ParseOK([("tag1", True), ("tag2", False)], processed=2))
        with self.subTest():
            self.assertEqual(p.MapDict(p.Seq([p.Tag("tag1", p.Bool()),
                                                    p.Tag("tag2", p.Bool())]))
                             .parse(["true", "false"]),
                             p.ParseOK({"tag1": True, "tag2": False}, processed=2))
        with self.subTest():
            self.assertEqual(p.MapDict(p.Seq([p.Tag("tag1", p.Bool().any()),
                                                    p.Tag("tag2", p.AnyStr().any())]))
                             .parse(["true", "moi"]),
                             p.ParseOK({"tag1": True, "tag2": "moi"}, processed=2))

    def test_one_of(self) -> None:
        with self.subTest():
            self.assertEqual(p.OneOf([p.Tag("tag1", p.Bool().any()),
                                         p.Tag("tag2", p.AnyStr().any())])
                             .parse(["moi"]),
                             p.ParseOK(("tag2", "moi"), processed=1))
        with self.subTest():
            self.assertEqual(p.OneOf([p.Tag("tag1", p.Bool().any()),
                                         p.Tag("tag2", p.AnyStr().any())])
                             .parse(["true"]),
                             p.ParseOK(("tag1", True), processed=1))
        with self.subTest():
            self.assertEqual(p.OneOfStrings(["moi", "hei"])
                             .parse(["true"]),
                             p.ParseFail("Expected one of moi, hei"))
        with self.subTest():
            self.assertEqual(p.OneOfStrings(["moi", "hei"])
                             .parse(["moi"]),
                             p.ParseOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(p.OneOfStrings(["moi", "hei"])
                             .parse(["hei"]),
                             p.ParseOK("hei", processed=1))

    def test_map_dict(self) -> None:
        with self.subTest():
            self.assertEqual(p.MapDict(p.SomeOf([p.Tag("tag1", p.Bool().any()),
                                                       p.Tag("tag2", p.AnyStr().any())]))
                             .parse(["moi"]),
                             p.ParseOK({"tag2": "moi"}, processed=1))
        with self.subTest():
            self.assertEqual(p.MapDict(p.SomeOf([p.Tag("tag1", p.Bool().any()),
                                                       p.Tag("tag2", p.AnyStr().any())]))
                             .parse(["true", "moi"]),
                             p.ParseOK({"tag1": True, "tag2": "moi"}, processed=2))

    def test_lambda(self) -> None:
        with self.subTest():
            def fixed() -> p.Parser[str]:
                return p.FixedStr("moi")
            self.assertEqual(p.Delayed(fixed).parse(["moi"]),
                             p.ParseOK("moi", processed=1))

    def test_hhmm(self) -> None:
        with self.subTest():
            self.assertEqual(p.HourMinute().parse([]),
                             p.ParseFail("No argument provided"))
        with self.subTest():
            self.assertEqual(p.HourMinute().parse([":00"]),
                             p.ParseFail("Failed to parse hh:mm"))
        with self.subTest():
            self.assertEqual(p.HourMinute().parse(["0:0"]),
                             p.ParseFail("Failed to parse hh:mm"))
        with self.subTest():
            self.assertEqual(p.HourMinute().parse(["00:00"]),
                             p.ParseOK((00, 00), processed=1))
        with self.subTest():
            self.assertEqual(p.HourMinute().parse(["24:00"]),
                             p.ParseFail("Hour cannot be >23"))
        with self.subTest():
            self.assertEqual(p.HourMinute().parse(["00:70"]),
                             p.ParseFail("Minute cannot be >59"))
