import re
import datetime
from abc import ABC, abstractmethod
from typing import List, Callable, Coroutine, Any, TypeVar, Generic, Optional, Tuple, Mapping, Union, Type, cast
from typing_extensions import Protocol
from enum import Enum
from dataclasses import dataclass

from .utils import coalesce, map_optional, round_to_next_second

class Unknown(ABC):
    """Used to represent unknown types; you get the actual type out from this with instanceof or casting"""
    pass

Parsed = TypeVar("Parsed")
T = TypeVar("T")
T1 = TypeVar("T1")
T2 = TypeVar("T2")
TagT = TypeVar('TagT')
class ParseResult(ABC, Generic[Parsed]):
    def __str__(self) -> str:
        return self.__repr__()

    @abstractmethod
    def __repr__(self) -> str:
        pass

    def __eq__(self, other: object) -> bool:
        if isinstance(self, ParseOK) and isinstance(other, ParseOK):
            return (self.value, self.processed) == (other.value, other.processed)
        if isinstance(self, ParseFail) and isinstance(other, ParseFail):
            return self.message == other.message
        return False

class ParseOK(ParseResult[Parsed]):
    value: Parsed
    """Result of parsing"""

    processed: int
    """How many args from invocation.args were processed"""

    def __init__(self, value: Parsed, processed: int) -> None:
        self.value = value
        self.processed = processed

    def __repr__(self) -> str:
        value = str(self.value)
        if isinstance(self.value, str):
            value = f"\"{value}\""
        return f"ParseOK({value}, {self.processed})"

class ParseFail(ParseResult[Parsed]):
    message: str
    def __init__(self, message: str):
        self.message = message

    def __repr__(self) -> str:
        return f"ParseFail(\"{self.message}\")"

class Parser(ABC, Generic[Parsed]):
    def __call__(self, args: List[str]) -> ParseResult[Parsed]:
        return self.parse(args)

    @abstractmethod
    def parse(self, args: List[str]) -> ParseResult[Parsed]:
        pass

    def base(self) -> "Parser[Parsed]":
        return self

    def any(self) -> "Parser[Any]":
        return self

    def unknown(self) -> "Parser[Unknown]":
        return cast(Parser[Unknown], self)

EmptyVal = Tuple[()]

class Empty(Parser[EmptyVal]):
    def parse(self, args: List[str]) -> ParseResult[EmptyVal]:
        if len(args) == 0:
            return ParseOK((), processed=0)
        else:
            return ParseFail("Expected no more arguments")

class AnyStr(Parser[str]):
    def parse(self, args: List[str]) -> ParseResult[str]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        return ParseOK(args[0], processed=1)

class RestAsStr(Parser[str]):
    def parse(self, args: List[str]) -> ParseResult[str]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        return ParseOK(" ".join(args), processed=len(args))

class List_(Parser[List[T]]):
    parser: Parser[T]

    def __init__(self, parser: Parser[T]) -> None:
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[List[T]]:
        parses: List[T] = []
        processed = 0
        while processed < len(args):
            parse = self.parser(args[processed:])
            if isinstance(parse, ParseOK):
                if parse.processed == 0:
                    return ParseFail(f"RestAsList subparser returned empty parse, cannot iterate list")
                parses.append(parse.value)
                processed += parse.processed
            else:
                break
        return ParseOK(parses, processed=processed)

class CaptureFixedStr(Parser[str]):
    fixed_string: str

    def __init__(self, fixed_string: str) -> None:
        self.fixed_string = fixed_string

    def parse(self, args: List[str]) -> ParseResult[str]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        if args[0].lower() == self.fixed_string.lower():
            return ParseOK(args[0], processed=1)
        else:
            return ParseFail(f"Expected {self.fixed_string}")

class Keyword(Parser[T]):
    """Non-capturing fixed str + simple Adjacent rolled in one

    Less outputs (and types) than a combinator would be.
    """
    keyword: str
    parser: Parser[T]

    def __init__(self, keyword: str, parser: Parser[T]) -> None:
        self.keyword = keyword
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[T]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        if args[0].lower() == self.keyword.lower():
            parse = self.parser(args[1:])
            if isinstance(parse, ParseFail):
                return parse
            assert isinstance(parse, ParseOK)
            return ParseOK(value=parse.value, processed=1+parse.processed)
        else:
            return ParseFail(f"Expected {self.keyword}")

class Regex(Parser[Tuple[str, ...]]):
    regex: "re.Pattern[str]"
    groups: List[Union[int, str]]

    def __init__(self, regex: str, groups: List[Union[int, str]]) -> None:
        self.regex = re.compile(regex)
        self.groups = groups

    def parse(self, args: List[str]) -> ParseResult[Tuple[str, ...]]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        match = re.match(self.regex, args[0])
        if match:
            return ParseOK(match.group(*self.groups), processed=1)
        else:
            return ParseFail(f"Failed to match regex {self.regex} with {args[0]}")

class Int(Parser[int]):
    parser: Regex

    def __init__(self) -> None:
        super().__init__()
        self.parser = Regex(r"[0-9]+", [0])

    def parse(self, args: List[str]) -> ParseResult[int]:
        result = self.parser.parse(args)
        if isinstance(result, ParseOK):
            return ParseOK(int(result.value[0]), processed=result.processed)
        else:
            assert isinstance(result, ParseFail)
            return ParseFail(result.message)

class Bool(Parser[bool]):
    def parse(self, args: List[str]) -> ParseResult[bool]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        value = args[0].lower()
        if ["on", "true", "1"].count(value):
            return ParseOK(True, processed=1)
        elif ["off", "false", "0"].count(value):
            return ParseOK(False, processed=1)
        else:
            return ParseFail(f"Invalid argument \"{args[0]}\" for boolean")

class Optional_(Parser[Optional[T]]):
    parser: Parser[T]

    def __init__(self, parser: Parser[T]) -> None:
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[Optional[T]]:
        result = self.parser.parse(args)
        if isinstance(result, ParseOK):
            return ParseOK(result.value, processed=result.processed)
        else:
            return ParseOK(None, processed=0)

class ValidOrMissing(Parser[Optional[T]]):
    parser: Parser[T]

    def __init__(self, parser: Parser[T]) -> None:
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[Optional[T]]:
        if len(args) == 0:
            return ParseOK(None, processed=0)
        else:
            result = self.parser.parse(args)
            if isinstance(result, ParseOK):
                return ParseOK(result.value, processed=result.processed)
            else:
                assert isinstance(result, ParseFail)
                return ParseFail(result.message)

CT = TypeVar('CT', contravariant=True)

class CallbackProtocol(Generic[CT], Protocol):
    def __call__(self, args: CT) -> None:
        pass

class Callback(Generic[T], Parser[Callable[[], None]]):
    parser: Parser[T]

    def __init__(self, callback: CallbackProtocol[T], parser: Parser[T]) -> None:
        self.parser = parser
        self.callback = callback

    def parse(self, args: List[str]) -> ParseResult[Callable[[], None]]:
        result = self.parser.parse(args)
        if isinstance(result, ParseOK):
            # mypy is confused about result.value?!
            return ParseOK(lambda: self.callback(result.value), processed=result.processed) # type: ignore
        else:
            assert isinstance(result, ParseFail)
            return ParseFail(message=result.message)

class Capture(Parser[Tuple[List[str], T]]):
    parser: Parser[T]

    def __init__(self, parser: Parser[T]) -> None:
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[Tuple[List[str], T]]:
        result = self.parser.parse(args)
        if isinstance(result, ParseOK):
            return ParseOK((args[0:result.processed], result.value), processed=result.processed)
        else:
            assert isinstance(result, ParseFail)
            return ParseFail(message=result.message)

class CaptureOnly(Parser[List[str]]):
    parser: Parser[Any]

    def __init__(self, parser: Parser[Any]) -> None:
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[List[str]]:
        result = self.parser.parse(args)
        if isinstance(result, ParseOK):
            return ParseOK(args[0:result.processed], processed=result.processed)
        else:
            assert isinstance(result, ParseFail)
            return result

class Map(Generic[T1, T2], Parser[T2]):
    parser: Parser[T1]
    map: List[Callable[[T1], T2]]

    def __init__(self, parser: Parser[T1], map: Callable[[T1], T2]) -> None:
        self.parser = parser
        self.map = [map]

    def parse(self, args: List[str]) -> ParseResult[T2]:
        result = self.parser.parse(args)
        if isinstance(result, ParseOK):
            return ParseOK(self.map[0](result.value), processed=result.processed)
        else:
            assert isinstance(result, ParseFail)
            return ParseFail(result.message)

class MapDict(Map[List[Tuple[TagT, T]], Mapping[TagT, T]]):
    parser: Parser[List[Tuple[TagT, T]]]

    def __init__(self, parser: Parser[List[Tuple[TagT, T]]]) -> None:
        def mapping(xs: List[Tuple[TagT, T]]) -> Mapping[TagT, T]:
            return dict(xs)
        super().__init__(parser, map=mapping)

class Tag(Map[T, Tuple[TagT, T]]):
    """Maps the result so that it is preceded by the given tag (in a 2-tuple)

    Can be useful with VldSomeOf for identifying which values came back.
    """

    tag: TagT

    def __init__(self, tag: TagT, parser: Parser[T]) -> None:
        def mapping(x: T) -> Tuple[TagT, T]:
            return (tag, x)
        super().__init__(parser, map=mapping)
        self.tag = tag

@dataclass
class Wrapped(Generic[T]):
    value: T

Wrapper = Callable[[T], Wrapped[T]]

class Wrap(Map[T, Wrapped[T]]):
    """Maps the result so that it is preceded by the given tag (in a 2-tuple)

    Can be useful with VldSomeOf for identifying which values came back.
    """

    def __init__(self, wrapper: Wrapper[T], parser: Parser[T]) -> None:
        def mapping(x: T) -> Wrapped[T]:
            return wrapper(x)
        super().__init__(parser, map=mapping)

def try_parses(parses: List[Callable[[], ParseResult[T]]]) -> ParseResult[T]:
    value = None
    for parse in parses:
        value = parse()
        if isinstance(value, ParseOK):
            return value
    assert value
    return value

class Adjacent(Parser[Tuple[T1, T2]]):
    """Parses two values in the same order as the given parsers

    It tries all combinations and prefers longest matches. E.g.  for
    [1, 2, 3] the left parser will be tried for all [1, 2, 3], [1, 2],
    [1] and [] and if some of these matches, then try the right parser.

    Similarly in right priority mode it first tries [1, 2, 3], [2, 3],
    [3], and [], and left parser for the result. However, the left
    parser must consume all between the left and the right parser.
    """

    parser_left: Parser[T1]
    parser_right: Parser[T2]
    right_priority: bool

    def __init__(self,
                 parser_left: Parser[T1],
                 parser_right: Parser[T2],
                 right_priority: bool = False) -> None:
        self.parser_left = parser_left
        self.parser_right = parser_right
        self.right_priority = right_priority

    def parse(self, args: List[str]) -> ParseResult[Tuple[T1, T2]]:
        def right_priority() -> ParseResult[Tuple[T1, T2]]:
            def try_with_right(sub_args: List[str], require_max_len: bool) -> ParseResult[Tuple[T1, T2]]:
                # For all sequences of length [0..len(args)[ find the longest one that can be parsed
                # sequentially by the two parsers provided. O(n^2).
                while True:
                    # find the longest sequence from the right that is parsed by parser_right that consumes
                    # all of the sequence
                    found_max_len = None
                    right = None
                    for max_len in range(len(sub_args), -1, -1):
                        right = self.parser_right.parse(sub_args[len(sub_args) - max_len:])
                        if isinstance(right, ParseOK) and (not require_max_len or right.processed == max_len):
                            found_max_len = max_len
                            break

                    if found_max_len is not None:
                        assert isinstance(right, ParseOK)
                        left = self.parser_left.parse(sub_args[0:len(sub_args) - found_max_len])
                        if isinstance(left, ParseFail):
                            return ParseFail(f"{left.message} while parsing left argument")
                        assert isinstance(left, ParseOK)
                        # there must be no gap between left and right parses
                        if left.processed == len(sub_args) - found_max_len:
                            break
                    if sub_args == []:
                        return ParseFail(f"No adjacent arguments parsed completely")
                    sub_args = sub_args[0:len(sub_args) - 1]
                return ParseOK((left.value, right.value), processed=left.processed + right.processed)
            return try_parses([lambda: try_with_right(args, True),
                               lambda: try_with_right(args, False)])

        def left_priority() -> ParseResult[Tuple[T1, T2]]:
            def try_with_left(sub_args: List[str], require_max_len: bool) -> ParseResult[Tuple[T1, T2]]:
                while True:
                    found_max_len = None
                    left = None
                    for max_len in range(len(sub_args), -1, -1):
                        left = self.parser_left.parse(sub_args[0:max_len])
                        if isinstance(left, ParseOK) and (not require_max_len or left.processed == max_len):
                            found_max_len = left.processed
                            break

                    if found_max_len is not None:
                        assert isinstance(left, ParseOK)
                        right = self.parser_right.parse(sub_args[found_max_len:])
                        if isinstance(right, ParseFail):
                            return ParseFail(f"{right.message} while parsing right argument")
                        assert isinstance(right, ParseOK)
                        break
                    if sub_args == []:
                        return ParseFail(f"No adjacent arguments parsed completely")
                    sub_args = sub_args[0:len(sub_args) - 1]
                return ParseOK((left.value, right.value), processed=left.processed + right.processed)
            return try_parses([lambda: try_with_left(args, True),
                               lambda: try_with_left(args, False)])

        if self.right_priority:
            return right_priority()
        else:
            return left_priority()

class IfThen(Parser[T]):
    """Adjacent, but ignores the capture of the left-side parser"""
    parser: Adjacent[Any, T]

    def __init__(self, parser_left: Parser[Any], parser_right: Parser[T]) -> None:
        self.parser = Adjacent(parser_left, parser_right)

    def parse(self, args: List[str]) -> ParseResult[T]:
        parse = self.parser(args)
        if isinstance(parse, ParseFail):
            return ParseFail(message=parse.message)
        assert isinstance(parse, ParseOK)
        return ParseOK(value=parse.value[1], processed=parse.processed)

class Remaining(Parser[T]):
    """Requires the underlying parser to parse all provided data, otherwise returns ParseFail"""
    parser: Parser[T]

    def __init__(self, parser: Parser[T]):
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[T]:
        result = self.parser.parse(args)
        if isinstance(result, ParseOK):
            if result.processed == len(args):
                return result
            else:
                return ParseFail("Extraneous input after command")
        else:
            return result

class Concat(Parser[str]):
    """Takes all provided input strings and concatenates them to one string"""
    def parse(self, args: List[str]) -> ParseResult[str]:
        return ParseOK(' '.join(args), processed=len(args))

class Seq(Parser[List[T]]):
    """Parser a sequence of values in the same order as the given parsers

    This requires all the parsers to be of the same type. `Parser.any()` can be useful for
    achieving this, but you lose static type checking. Alternatively you can use `VldMap` to map
    the types.

    If you end up typing different types in the same list, you may find `Parser.base()` useful
    for the upcasting.
    """

    parsers: List[Parser[T]]

    def __init__(self, parsers: List[Parser[T]]) -> None:
        assert parsers, "VldSeq: expected at least one parser"
        self.parsers = parsers

    def parse(self, args: List[str]) -> ParseResult[List[T]]:
        results: List[T] = []
        total_processed = 0
        for index, parser in enumerate(self.parsers):
            result = parser.parse(args)
            if isinstance(result, ParseFail):
                return ParseFail(f"{result.message} while parsing argument {index + 1}")
            assert(isinstance(result, ParseOK))
            results.append(result.value)
            total_processed += result.processed
            args = args[result.processed:]
        assert results
        return ParseOK(results, processed=total_processed)

class OneOf(Parser[T]):
    parsers: Tuple[Parser[T], ...]

    def __init__(self, *parsers: Parser[T]) -> None:
        self.parsers = parsers

    def parse(self, args: List[str]) -> ParseResult[T]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        for parser in self.parsers:
            result = parser.parse(args)
            if isinstance(result, ParseOK):
                return result
        return ParseFail(f"Invalid value")

class OneOfStrings(Parser[str]):
    strings: List[str]

    def __init__(self, strings: List[str]) -> None:
        self.strings = strings

    def parse(self, args: List[str]) -> ParseResult[str]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        if [str.lower() for str in self.strings].count(args[0].lower()):
            return ParseOK(args[0], processed=1)
        else:
            valid_values = ", ".join(self.strings)
            return ParseFail(f"Expected one of {valid_values}")

TEnum = TypeVar('TEnum', bound=Enum)

class OneOfEnumValue(Generic[TEnum], Parser[TEnum]):
    enum: Type[TEnum]

    def __init__(self, enum: Type[TEnum]) -> None:
        self.enum = enum

    def parse(self, args: List[str]) -> ParseResult[TEnum]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        values = [enum for enum in self.enum.__members__.values()
                  if enum.value.lower() == args[0].lower()]
        if values:
            return ParseOK(values[0], processed=1)
        else:
            strings = [enum.value for enum in self.enum.__members__.values()]
            valid_values = ", ".join(strings)
            return ParseFail(f"Expected one of {valid_values}")

class Delayed(Parser[T]):
    mk_validator: List[Callable[[], Parser[T]]]

    def __init__(self, parser: Callable[[], Parser[T]]):
        self.parser = parser

    def parse(self, args: List[str]) -> ParseResult[T]:
        return self.parser().parse(args)

class SomeOf(Parser[List[T]]):
    """Parses a sequence of values with given parsers, but the order of the values can be anything
    and they can also be omitted in part or completely."""

    parsers: List[Parser[T]]

    def __init__(self, parsers: List[Parser[T]]) -> None:
        assert parsers, "VldSeq: expected at least one parser"
        self.parsers = parsers

    def parse(self, args: List[str]) -> ParseResult[List[T]]:
        parsers = self.parsers
        total_processed = 0
        any_matched = True
        results: List[T] = []
        while any_matched:
            any_matched = False
            next_parsers: List[Parser[T]] = []
            for parser in parsers:
                result = parser.parse(args)
                if isinstance(result, ParseOK):
                    results.append(result.value)
                    total_processed += result.processed
                    args = args[result.processed:]
                    any_matched = True
                else:
                    next_parsers.append(parser)
            parsers = next_parsers
        return ParseOK(results, processed=total_processed)

class Interval(Parser[datetime.timedelta]):
    regex: Regex

    def __init__(self) -> None:
        self.regex = Regex(r"^(?:([0-9]{1,4})h)?(?:([0-9]{1,4})m)?$", [1, 2])

    def parse(self, args: List[str]) -> ParseResult[datetime.timedelta]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        result = self.regex.parse(args)
        if isinstance(result, ParseFail):
            return ParseFail("Failed to parse time interval")
        assert(isinstance(result, ParseOK))
        hours = result.value[0]
        minutes = result.value[1]
        if hours is None and minutes is None:
            return ParseFail("Failed to parse time interval")
        delta = datetime.timedelta(hours=coalesce(map_optional(hours, int), 0),
                                   minutes=coalesce(map_optional(minutes, int), 0))
        if delta.total_seconds() < 60:
            return ParseFail("Too short interval")
        return ParseOK(delta, processed=result.processed)

class Time(Parser[datetime.datetime]):
    regex: Regex
    now: Optional[datetime.datetime]

    def __init__(self, now: Optional[datetime.datetime] = None) -> None:
        self.regex = Regex(r"^(?:([0-9]{1,2}):?([0-9]{2})|(?:([0-9]{1,4})h)?(?:([0-9]{1,4})m)?)$", [1, 2, 3, 4])
        self.now = now

    def parse(self, args: List[str]) -> ParseResult[datetime.datetime]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        result = self.regex.parse(args)
        if isinstance(result, ParseFail):
            return ParseFail("Failed to parse hh:mm")
        else:
            assert isinstance(result, ParseOK)
            now = coalesce(self.now, datetime.datetime.now())
            if now.microsecond:
                # round to next second
                now = round_to_next_second(now)
            if result.value[0] is not None:
                hh, mm = (int(result.value[0]), int(result.value[1]))
                if hh is not None and hh > 23:
                    return ParseFail("Hour cannot be >23")
                elif mm > 59:
                    return ParseFail("Minute cannot be >59")
                date = coalesce(map_optional(self.now, lambda x: x.date()), datetime.date.today())
                time_of_day = datetime.time(hh, mm)
                time = datetime.datetime.combine(date, time_of_day)
                while time < now:
                    time += datetime.timedelta(days=1)
            else:
                hours = result.value[2]
                minutes = result.value[3]
                if hours is None and minutes is None:
                    return ParseFail("Failed to parse relative time")
                delta = datetime.timedelta(hours=coalesce(map_optional(hours, int), 0),
                                           minutes=coalesce(map_optional(minutes, int), 0))
                time = now + delta
            return ParseOK(time, processed=result.processed)
