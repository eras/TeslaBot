import asyncio
import unittest
import datetime
from typing import List, TypeVar, Optional, Tuple
from enum import Enum

import teslabot.parser as p

Result = TypeVar('Result', bound=object)

class TestParser(unittest.TestCase):
    def __init__(self, method_name: str) -> None:
        super().__init__(method_name)
        self.longMessage = True

    def test_simple_parsers(self) -> None:
        with self.subTest():
            self.assertEqual(p.Empty().parse([]),
                             p.ParseOK((), processed=0))
        with self.subTest():
            self.assertEqual(p.Empty().parse(["hei"]),
                             p.ParseFail("Expected no more arguments", processed=0))
        with self.subTest():
            self.assertEqual(p.AnyStr().parse([]),
                             p.ParseFail("No argument provided", processed=0))
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
                             p.ParseFail("No argument provided", processed=0))
        with self.subTest():
            self.assertEqual(p.Bool().parse([""]),
                             p.ParseFail('Invalid argument "" for boolean', processed=0))
        with self.subTest():
            self.assertEqual(p.Bool().parse(["moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean', processed=0))
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
            self.assertEqual(p.Regex(r"(.*)").parse([""]),
                             p.ParseOK(("",), processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r"(.*)").parse(["moi"]),
                             p.ParseOK(("moi",), processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r"(.(oi))").parse(["moi"]),
                             p.ParseOK(("moi","oi"), processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r".(oi)").parse(["moi"]),
                             p.ParseOK(("oi",), processed=1))
        with self.subTest():
            self.assertEqual(p.Regex(r".(o)(i)").parse(["moi"]),
                             p.ParseOK(("o", "i"), processed=1))

    def test_map(self) -> None:
        def negate(x: bool) -> bool:
            return not x
        with self.subTest():
            self.assertEqual(p.Map(p.Bool(), negate).parse([]),
                             p.ParseFail("No argument provided", processed=0))
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
                             p.ParseFail('Invalid argument "moi" for boolean', processed=0))
        with self.subTest():
            self.assertEqual(p.ValidOrMissing(p.Bool()).parse(["true"]),
                             p.ParseOK(True, processed=1))

    def test_adjacent(self) -> None:
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse([]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool(),
                                        right_priority=True).parse([]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                           p.Bool()).parse(["moi"]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool(),
                                        right_priority=True).parse(["moi"]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool()).parse(["moi", "1"]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool(),
                                        right_priority=True).parse(["moi", "1"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing left argument', processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool()).parse(["1"]),
                             p.ParseFail("No argument provided while parsing right argument", processed=1))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool(),
                                        right_priority=True).parse(["1"]),
                             p.ParseFail("No argument provided while parsing left argument", processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool()).parse(["1", "moi"]),
                             # not optimal.. we should have a flag for requiring non-empty parse attempts?
                             # or just choose an error from one if possible?
                             p.ParseFail('Invalid argument "moi" for boolean while parsing right argument', processed=1))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool(),
                                        right_priority=True).parse(["1", "moi"]),
                             # confusing? but correct, I suppose it tried right(["1", "moi"]) which was
                             # partial parse and then there was nothing to parse on the left. parsing
                             # right(["moi"]) failed do left parser was never tried for that.
                             p.ParseFail("No argument provided while parsing left argument", processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool()).parse(["0", "1"]),
                             p.ParseOK((False, True), processed=2))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Bool(),
                                        right_priority=True).parse(["0", "1"]),
                             p.ParseOK((False, True), processed=2))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Adjacent(p.Bool(),
                                                   p.Empty())).parse(["0", "1"]),
                             p.ParseOK((False, (True, ())), processed=2))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Bool(),
                                        p.Adjacent(p.Bool(),
                                                   p.Empty()),
                                        right_priority=True).parse(["0", "1"]),
                             p.ParseOK((False, (True, ())), processed=2))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Remaining(p.Concat()),
                                        p.Remaining(p.Concat()),
                                        right_priority=False).parse(["0", "1"]),
                             p.ParseOK(("0 1", ""), processed=2))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Remaining(p.Concat()),
                                        p.Remaining(p.Concat()),
                                        right_priority=True).parse(["0", "1"]),
                             p.ParseOK(("", "0 1"), processed=2))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Remaining(p.Concat()),
                                        p.Remaining(p.Concat()),
                                        right_priority=True).parse(["0", "1", "2"]),
                             p.ParseOK(("", "0 1 2"), processed=3))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Remaining(p.Concat()),
                                        p.Remaining(p.AnyStr()),
                                        right_priority=True).parse(["0", "1", "2"]),
                             p.ParseOK(("0 1", "2"), processed=3))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.Remaining(p.Concat()),
                                        p.Remaining(p.Optional_(p.AnyStr())),
                                        right_priority=True).parse(["0", "1", "2"]),
                             p.ParseOK(("0 1", "2"), processed=3))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.ValidOrMissing(p.AnyStr()),
                                        p.Empty()).parse([]),
                             p.ParseOK((None, ()), processed=0))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.ValidOrMissing(p.AnyStr()),
                                        p.Empty()).parse(["0"]),
                             p.ParseOK(("0", ()), processed=1))
        with self.subTest():
            self.assertEqual(p.Adjacent(p.ValidOrMissing(p.AnyStr()),
                                        p.Empty()).parse(["0", "1"]),
                             p.ParseFail("Expected no more arguments while parsing right argument", processed=1))

    def test_remaining(self) -> None:
        with self.subTest():
            self.assertEqual(p.Remaining(p.Bool()).parse([]),
                             p.ParseFail("No argument provided", processed=0))

        with self.subTest():
            self.assertEqual(p.Remaining(p.Bool()).parse(["false"]),
                             p.ParseOK(False, processed=1))

        with self.subTest():
            self.assertEqual(p.Remaining(p.Bool()).parse(["false", "false"]),
                             p.ParseFail("Extraneous input after command", processed=1))

    def test_seq(self) -> None:
        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse([]),
                             p.ParseFail("No argument provided while parsing argument 1", processed=0))

        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse(["moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing argument 1', processed=0))

        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse(["moi", "moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing argument 1', processed=0))

        with self.subTest():
            self.assertEqual(p.Seq([p.Bool(),
                                       p.Bool()]).parse(["1", "moi"]),
                             p.ParseFail('Invalid argument "moi" for boolean while parsing argument 2', processed=1))

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
                             p.ParseFail("No argument provided", processed=0))
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

    def test_wrap(self) -> None:
        with self.subTest():
            self.assertEqual(p.Wrap(lambda a: p.Wrapped(a),
                                    p.Bool()).parse([]),
                             p.ParseFail("No argument provided", processed=0))

        with self.subTest():
            self.assertEqual(p.Wrap(lambda a: p.Wrapped(a),
                                    p.Bool()).parse(["true"]),
                             p.ParseOK(p.Wrapped(True), processed=1))

        with self.subTest():
            self.assertEqual(p.Wrap(p.Wrapped,
                                    p.Bool()).parse(["true"]),
                             p.ParseOK(p.Wrapped(True), processed=1))

        class CustomWrapped(p.Wrapped[p.T]):
            pass

        with self.subTest():
            self.assertEqual(p.Wrap(CustomWrapped,
                                    p.Bool()).parse(["true"]),
                             p.ParseOK(CustomWrapped(True), processed=1))

    def test_one_of(self) -> None:
        with self.subTest():
            self.assertEqual(p.OneOf(p.Tag("tag1", p.Bool().any()),
                                     p.Tag("tag2", p.AnyStr().any()))
                             .parse(["moi"]),
                             p.ParseOK(("tag2", "moi"), processed=1))
        with self.subTest():
            self.assertEqual(p.OneOf(p.Tag("tag1", p.Bool().any()),
                                     p.Tag("tag2", p.AnyStr().any()))
                             .parse(["true"]),
                             p.ParseOK(("tag1", True), processed=1))
        with self.subTest():
            self.assertEqual(p.OneOfStrings(["moi", "hei"])
                             .parse(["true"]),
                             p.ParseFail("Expected one of moi, hei", processed=0))
        with self.subTest():
            self.assertEqual(p.OneOfStrings(["moi", "hei"])
                             .parse(["moi"]),
                             p.ParseOK("moi", processed=1))
        with self.subTest():
            self.assertEqual(p.OneOfStrings(["moi", "hei"])
                             .parse(["hei"]),
                             p.ParseOK("hei", processed=1))

    def test_one_of_enum(self) -> None:
        class Test(Enum):
            a = "Hello"
            b = "world"

        with self.subTest():
            self.assertEqual(p.OneOfEnumValue(Test).parse([]),
                             p.ParseFail("No argument provided", processed=0))
        with self.subTest():
            self.assertEqual(p.OneOfEnumValue(Test).parse(["not"]),
                             p.ParseFail("Expected one of Hello, world", processed=0))
        with self.subTest():
            self.assertEqual(p.OneOfEnumValue(Test).parse(["Hello"]),
                             p.ParseOK(Test.a, processed=1))
        with self.subTest():
            self.assertEqual(p.OneOfEnumValue(Test).parse(["hello"]),
                             p.ParseOK(Test.a, processed=1))
        with self.subTest():
            self.assertEqual(p.OneOfEnumValue(Test).parse(["world"]),
                             p.ParseOK(Test.b, processed=1))


    def test_some_of(self) -> None:
        with self.subTest():
            self.assertEqual(p.SomeOf(p.Tag("tag1", p.Bool().any()),
                                      p.Tag("tag2", p.AnyStr().any()))
                             .parse(["moi"]),
                             p.ParseOK((None, ('tag2', 'moi')), processed=1))
        with self.subTest():
            self.assertEqual(p.SomeOf(p.Tag("tag1", p.Bool().any()),
                                      p.Tag("tag2", p.AnyStr().any()))
                             .parse(["true", "moi"]),
                             p.ParseOK((('tag1', True), ('tag2', 'moi')), processed=2))
        with self.subTest():
            self.assertEqual(p.SomeOf2(p.Tag("tag1", p.Bool()),
                                       p.Tag("tag2", p.AnyStr()))
                             .parse(["true", "moi"]),
                             p.ParseOK((('tag1', True), ('tag2', 'moi')), processed=2))
        with self.subTest():
            self.assertEqual(p.SomeOf3(p.Tag("tag3", p.Int()),
                                       p.Tag("tag1", p.Bool()),
                                       p.Tag("tag2", p.AnyStr()))
                             .parse(["true", "moi"]),
                             p.ParseOK((None, ('tag1', True), ('tag2', 'moi')), processed=2))
        with self.subTest():
            self.assertEqual(p.SomeOf4(p.Tag("tag3", p.Int()),
                                       p.Tag("tag1", p.Bool()),
                                       p.Tag("tag2", p.AnyStr()),
                                       p.Tag("tag4", p.AnyStr()))
                             .parse(["true", "moi", "tidii"]),
                             p.ParseOK((None, ('tag1', True), ('tag2', 'moi'), ('tag4', 'tidii')), processed=3))

    def test_lambda(self) -> None:
        with self.subTest():
            def fixed() -> p.Parser[str]:
                return p.CaptureFixedStr("moi")
            self.assertEqual(p.Delayed(fixed).parse(["moi"]),
                             p.ParseOK("moi", processed=1))


    def test_keyword(self) -> None:
        with self.subTest():
            self.assertEqual(p.Keyword("foo", p.Capture(p.AnyStr())).parse([]),
                             p.ParseFail("No argument provided", processed=0))
        with self.subTest():
            self.assertEqual(p.Keyword("foo", p.Capture(p.AnyStr())).parse(["moi"]),
                             p.ParseFail("Expected foo", processed=0))
        with self.subTest():
            self.assertEqual(p.Keyword("foo", p.Capture(p.AnyStr())).parse(["foo"]),
                             p.ParseFail("No argument provided after foo", processed=1))
        with self.subTest():
            self.assertEqual(p.Keyword("foo", p.Capture(p.AnyStr())).parse(["foo", "hei"]),
                             p.ParseOK((["hei"], "hei"), processed=2))

    def test_ifthen(self) -> None:
        with self.subTest():
            self.assertEqual(p.IfThen(p.CaptureFixedStr("foo"),
                                      p.Capture(p.AnyStr())).parse([]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))
        with self.subTest():
            self.assertEqual(p.IfThen(p.CaptureFixedStr("foo"),
                                      p.Capture(p.AnyStr())).parse(["moi"]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))
        with self.subTest():
            self.assertEqual(p.IfThen(p.CaptureFixedStr("foo"),
                                      p.Capture(p.AnyStr())).parse(["foo"]),
                             p.ParseFail("No argument provided while parsing right argument", processed=1))
        with self.subTest():
            self.assertEqual(p.IfThen(p.CaptureFixedStr("foo"),
                                      p.Capture(p.AnyStr())).parse(["foo", "hei"]),
                             p.ParseOK((["hei"], "hei"), processed=2))

    def test_interval(self) -> None:
        with self.subTest():
            self.assertEqual(p.Interval().parse([]),
                             p.ParseFail("No argument provided", processed=0))
        with self.subTest():
            self.assertEqual(p.Interval().parse([""]),
                             p.ParseFail("Failed to parse time interval", processed=0))
        with self.subTest():
            self.assertEqual(p.Interval().parse(["0h"]),
                             p.ParseFail("Too short interval", processed=0))
        with self.subTest():
            self.assertEqual(p.Interval().parse(["1h"]),
                             p.ParseOK(datetime.timedelta(hours=1), processed=1))
        with self.subTest():
            self.assertEqual(p.Interval().parse(["1m"]),
                             p.ParseOK(datetime.timedelta(minutes=1), processed=1))
        with self.subTest():
            self.assertEqual(p.Interval().parse(["1h1m"]),
                             p.ParseOK(datetime.timedelta(hours=1, minutes=1), processed=1))

    def test_time(self) -> None:
        now = datetime.datetime.fromisoformat("2022-02-22 01:00")
        today = now.date()
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse([]),
                             p.ParseFail("No argument provided", processed=0))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse([":00"]),
                             p.ParseFail("Failed to parse hh:mm", processed=0))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["0:0"]),
                             p.ParseFail("Failed to parse hh:mm", processed=0))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["00:00"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(0, 0)) +
                                       datetime.timedelta(days=1),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["01:00"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(1, 0)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["02:00"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(2, 0)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["0m"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(1, 0)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["10m"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(1, 10)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["60m"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(2, 0)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["0h"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(1, 0)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["12h"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(13, 0)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["12h12m"]),
                             p.ParseOK(datetime.datetime.combine(today,
                                                                 datetime.time(13, 12)),
                                       processed=1))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["24:00"]),
                             p.ParseFail("Hour cannot be >23", processed=0))
        with self.subTest():
            self.assertEqual(p.Time(now=now).parse(["00:70"]),
                             p.ParseFail("Minute cannot be >59", processed=0))

    def test_rest_as_list(self) -> None:
        with self.subTest():
            self.assertEqual(p.List_(p.AnyStr()).parse([]),
                             p.ParseOK([], processed=0))

        with self.subTest():
            self.assertEqual(p.List_(p.AnyStr()).parse(["hello"]),
                             p.ParseOK(["hello"], processed=1))

        with self.subTest():
            self.assertEqual(p.List_(p.Int()).parse(["1", "2"]),
                             p.ParseOK([1, 2], processed=2))

        with self.subTest():
            self.assertEqual(p.List_(p.Adjacent(p.Int(), p.Int())).parse(["1", "2"]),
                             p.ParseOK([(1, 2)], processed=2))

        with self.subTest():
            self.assertEqual(p.List_(p.Adjacent(p.Int(), p.Int())).parse(["1", "2", "3"]),
                             p.ParseOK([(1, 2)], processed=2))


        with self.subTest():
            self.assertEqual(p.List_(p.Adjacent(p.Int(), p.Int())).parse(["1", "2", "3", "moi"]),
                             p.ParseOK([(1, 2)], processed=2))

        with self.subTest():
            self.assertEqual(p.List_(p.Adjacent(p.Int(), p.Int())).parse(["1", "2", "3", "4"]),
                             p.ParseOK([(1, 2), (3, 4)], processed=4))

    def test_meters(self) -> None:
        with self.subTest():
            self.assertEqual(p.Meters().parse([]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))

        with self.subTest():
            self.assertEqual(p.Meters().parse(["hei"]),
                             p.ParseFail("No adjacent arguments parsed completely", processed=0))

        with self.subTest():
            self.assertEqual(p.Meters().parse(["1"]),
                             p.ParseFail("No argument provided while parsing right argument", processed=1))

        with self.subTest():
            self.assertEqual(p.Meters().parse(["1", "m"]),
                             p.ParseOK(1.0, processed=2))

        with self.subTest():
            self.assertEqual(p.Meters().parse(["1.0", "m"]),
                             p.ParseOK(1.0, processed=2))

        with self.subTest():
            self.assertEqual(p.Meters().parse(["1.0", "km"]),
                             p.ParseOK(1000.0, processed=2))

        with self.subTest():
            self.assertEqual(p.Meters().parse(["1.0", "mm"]),
                             p.ParseFail("Expected one of m, km while parsing right argument", processed=1))

        with self.subTest():
            self.assertEqual(p.Meters().parse(["1.0", "m", "z"]),
                             p.ParseOK(1.0, processed=2))
