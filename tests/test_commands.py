import asyncio
import unittest
from typing import List, TypeVar, Optional, Tuple

import teslabot.commands as c

Result = TypeVar('Result', bound=object)

class TestCommands(unittest.TestCase):
    def __init__(self, method_name: str) -> None:
        super().__init__(method_name)
        self.longMessage = True

    def setup_commands(self, called: List[Optional[Result]], valid: c.Validator[Result]) -> c.Commands[None]:
        cmds = c.Commands[None]()
        async def command0(context: None, valid: Result, args: List[str]) -> None:
            called[0] = valid
        async def command1(context: None, valid: Result, args: List[str]) -> None:
            called[1] = valid
        cmds.register(c.Function("test0", valid, command0))
        cmds.register(c.Function("test1", valid, command1))
        return cmds

    def test_simple_call(self) -> None:
        async def test() -> None:
            called: List[Optional[c.Empty]] = [None, None]
            cmds = self.setup_commands(called, c.VldEmpty())
            await cmds.invoke(None, c.Invocation(name="test0", args=[]))
            self.assertIsNotNone(called[0], "Command test0 was not called")
            self.assertIsNone(called[1], "Command test1 was called")
        asyncio.get_event_loop().run_until_complete(test())

    def test_validated_call(self) -> None:
        async def test() -> None:
            called: List[Optional[str]] = [None, None]
            cmds = self.setup_commands(called, c.VldAnyStr())
            await cmds.invoke(None, c.Invocation(name="test0", args=["arg1"]))
            self.assertEqual(called[0], "arg1", "Command test0 was not called")
            self.assertIsNone(called[1], "Command test1 was called")
        asyncio.get_event_loop().run_until_complete(test())

    def test_simple_validators(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldEmpty().validate([]),
                             c.ValidatorOK((), processed=0))
        with self.subTest():
            self.assertEqual(c.VldEmpty().validate(["hei"]),
                             c.ValidatorFail("Expected no more arguments"))
        with self.subTest():
            self.assertEqual(c.VldAnyStr().validate([]),
                             c.ValidatorFail("No argument provided"))
        with self.subTest():
            self.assertEqual(c.VldAnyStr().validate([""]),
                             c.ValidatorOK("", processed=1))
        with self.subTest():
            self.assertEqual(c.VldAnyStr().validate(["moi"]),
                             c.ValidatorOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(c.VldAnyStr().validate(["moi", "moi"]),
                             c.ValidatorOK("moi", processed=1))

    def test_bool(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldBool().validate([]),
                             c.ValidatorFail("No argument provided"))
        with self.subTest():
            self.assertEqual(c.VldBool().validate([""]),
                             c.ValidatorFail('Invalid argument "" for boolean'))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["moi"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean'))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["True"]),
                             c.ValidatorOK(True, processed=1))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["true"]),
                             c.ValidatorOK(True, processed=1))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["FALSE"]),
                             c.ValidatorOK(False, processed=1))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["false"]),
                             c.ValidatorOK(False, processed=1))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["0"]),
                             c.ValidatorOK(False, processed=1))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["1"]),
                             c.ValidatorOK(True, processed=1))
        with self.subTest():
            self.assertEqual(c.VldBool().validate(["off"]),
                             c.ValidatorOK(False, processed=1))
        with self.subTest(): self.assertEqual(c.VldBool().validate(["on"]),
                                              c.ValidatorOK(True, processed=1))

    def test_regex(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldRegex(r".*", [0]).validate([""]),
                             c.ValidatorOK("", processed=1))
        with self.subTest():
            self.assertEqual(c.VldRegex(r".*", [0]).validate(["moi"]),
                             c.ValidatorOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(c.VldRegex(r".(oi)", [0]).validate(["moi"]),
                             c.ValidatorOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(c.VldRegex(r".(oi)", [1]).validate(["moi"]),
                             c.ValidatorOK("oi", processed=1))
        with self.subTest():
            self.assertEqual(c.VldRegex(r".(o)(i)", [1, 2]).validate(["moi"]),
                             c.ValidatorOK(("o", "i"), processed=1))

    def test_map(self) -> None:
        def negate(x: bool) -> bool:
            return not x
        with self.subTest():
            self.assertEqual(c.VldMap(c.VldBool(), negate).validate([]),
                             c.ValidatorFail("No argument provided"))
        with self.subTest():
            self.assertEqual(c.VldMap(c.VldBool(), negate).validate(["true"]),
                             c.ValidatorOK(False, processed=1))
        with self.subTest():
            self.assertEqual(c.VldMap(c.VldBool(), negate).validate(["false"]),
                             c.ValidatorOK(True, processed=1))

    # def test_one_of(self) -> None:
    #     with self.subTest():
    #         self.assertEqual(c.VldOneOf("").validate([]),
    #                          c.ValidatorFail("No argument provided"))

    def test_optional(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldOptional(c.VldBool()).validate([]),
                             c.ValidatorOK(None, processed=0))
        with self.subTest():
            self.assertEqual(c.VldOptional(c.VldBool()).validate(["moi"]),
                             c.ValidatorOK(None, processed=0))
        with self.subTest():
            self.assertEqual(c.VldOptional(c.VldBool()).validate(["true"]),
                             c.ValidatorOK(True, processed=1))

        with self.subTest():
            self.assertEqual(c.VldValidOrMissing(c.VldBool()).validate([]),
                             c.ValidatorOK(None, processed=0))
        with self.subTest():
            self.assertEqual(c.VldValidOrMissing(c.VldBool()).validate(["moi"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean'))
        with self.subTest():
            self.assertEqual(c.VldValidOrMissing(c.VldBool()).validate(["true"]),
                             c.ValidatorOK(True, processed=1))

    def test_adjacent(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldAdjacent(c.VldBool(),
                                           c.VldBool()).validate([]),
                             c.ValidatorFail("No argument provided while parsing first argument"))
        with self.subTest():
            self.assertEqual(c.VldAdjacent(c.VldBool(),
                                           c.VldBool()).validate(["moi"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean while parsing first argument'))
        with self.subTest():
            self.assertEqual(c.VldAdjacent(c.VldBool(),
                                           c.VldBool()).validate(["moi", "1"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean while parsing first argument'))
        with self.subTest():
            self.assertEqual(c.VldAdjacent(c.VldBool(),
                                           c.VldBool()).validate(["1"]),
                             c.ValidatorFail("No argument provided while parsing second argument"))
        with self.subTest():
            self.assertEqual(c.VldAdjacent(c.VldBool(),
                                           c.VldBool()).validate(["1", "moi"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean while parsing second argument'))
        with self.subTest():
            self.assertEqual(c.VldAdjacent(c.VldBool(),
                                           c.VldBool()).validate(["1", "0"]),
                             c.ValidatorOK((True, False), processed=2))
        with self.subTest():
            self.assertEqual(c.VldAdjacent(c.VldBool(),
                                           c.VldAdjacent(c.VldBool(),
                                                         c.VldEmpty())).validate(["1", "0"]),
                             c.ValidatorOK((True, (False, ())), processed=2))

    def test_seq(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldSeq([c.VldBool(),
                                       c.VldBool()]).validate([]),
                             c.ValidatorFail("No argument provided while parsing argument 1"))

        with self.subTest():
            self.assertEqual(c.VldSeq([c.VldBool(),
                                       c.VldBool()]).validate(["moi"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean while parsing argument 1'))

        with self.subTest():
            self.assertEqual(c.VldSeq([c.VldBool(),
                                       c.VldBool()]).validate(["moi", "moi"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean while parsing argument 1'))

        with self.subTest():
            self.assertEqual(c.VldSeq([c.VldBool(),
                                       c.VldBool()]).validate(["1", "moi"]),
                             c.ValidatorFail('Invalid argument "moi" for boolean while parsing argument 2'))

        with self.subTest():
            self.assertEqual(c.VldSeq([c.VldBool(),
                                       c.VldBool()]).validate(["1", "0"]),
                             c.ValidatorOK([True, False], processed=2))
        with self.subTest():
            self.assertEqual(c
                             .VldSeq([c.VldSeq([c.VldMap(c.VldBool(), str).base(),
                                                c.VldMap(c.VldAnyStr(), str)]),
                                      c.VldMap[bool, List[str]](c.VldBool(), lambda x: [str(x)]),
                                      c.VldMap[c.Empty, List[str]](c.VldEmpty(), lambda _: [])])
                             .validate(["1", "moi", "0"]),
                             c.ValidatorOK([["True", "moi"], ["False"], []], processed=3))

        # this is probably more practical though less safely typed..
        with self.subTest():
            self.assertEqual(c
                             .VldSeq([c.VldSeq([c.VldBool().any(),
                                                c.VldAnyStr().any()]),
                                      c.VldBool().any(),
                                      c.VldEmpty().any()])
                             .validate(["1", "moi", "0"]),
                             c.ValidatorOK([[True, "moi"], False, ()], processed=3))

        with self.subTest():
            self.assertEqual(c.VldTag("tag", c.VldBool())
                             .validate([]),
                             c.ValidatorFail("No argument provided"))
        with self.subTest():
            self.assertEqual(c.VldTag("tag", c.VldBool())
                             .validate(["true"]),
                             c.ValidatorOK(("tag", True), processed=1))
        with self.subTest():
            self.assertEqual(c.VldSeq([c.VldTag("tag1", c.VldBool()),
                                       c.VldTag("tag2", c.VldBool())])
                             .validate(["true", "false"]),
                             c.ValidatorOK([("tag1", True), ("tag2", False)], processed=2))
        with self.subTest():
            self.assertEqual(c.VldMapDict(c.VldSeq([c.VldTag("tag1", c.VldBool()),
                                                    c.VldTag("tag2", c.VldBool())]))
                             .validate(["true", "false"]),
                             c.ValidatorOK({"tag1": True, "tag2": False}, processed=2))
        with self.subTest():
            self.assertEqual(c.VldMapDict(c.VldSeq([c.VldTag("tag1", c.VldBool().any()),
                                                    c.VldTag("tag2", c.VldAnyStr().any())]))
                             .validate(["true", "moi"]),
                             c.ValidatorOK({"tag1": True, "tag2": "moi"}, processed=2))

    def test_one_of(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldOneOf([c.VldTag("tag1", c.VldBool().any()),
                                         c.VldTag("tag2", c.VldAnyStr().any())])
                             .validate(["moi"]),
                             c.ValidatorOK(("tag2", "moi"), processed=1))
        with self.subTest():
            self.assertEqual(c.VldOneOf([c.VldTag("tag1", c.VldBool().any()),
                                         c.VldTag("tag2", c.VldAnyStr().any())])
                             .validate(["true"]),
                             c.ValidatorOK(("tag1", True), processed=1))
        with self.subTest():
            self.assertEqual(c.VldOneOfStrings(["moi", "hei"])
                             .validate(["true"]),
                             c.ValidatorFail("Expected one of moi, hei"))
        with self.subTest():
            self.assertEqual(c.VldOneOfStrings(["moi", "hei"])
                             .validate(["moi"]),
                             c.ValidatorOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(c.VldOneOfStrings(["moi", "hei"])
                             .validate(["hei"]),
                             c.ValidatorOK("hei", processed=1))

    def test_map_dict(self) -> None:
        with self.subTest():
            self.assertEqual(c.VldMapDict(c.VldSomeOf([c.VldTag("tag1", c.VldBool().any()),
                                                       c.VldTag("tag2", c.VldAnyStr().any())]))
                             .validate(["moi"]),
                             c.ValidatorOK({"tag2": "moi"}, processed=1))
        with self.subTest():
            self.assertEqual(c.VldMapDict(c.VldSomeOf([c.VldTag("tag1", c.VldBool().any()),
                                                       c.VldTag("tag2", c.VldAnyStr().any())]))
                             .validate(["true", "moi"]),
                             c.ValidatorOK({"tag1": True, "tag2": "moi"}, processed=2))

    def test_lambda(self) -> None:
        with self.subTest():
            def fixed() -> c.Validator[str]:
                return c.VldFixedStr("moi")
            self.assertEqual(c.VldDelayed(fixed).validate(["moi"]),
                             c.ValidatorOK("moi", processed=1))
