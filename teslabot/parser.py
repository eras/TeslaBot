import re
from abc import ABC, abstractmethod
from typing import List, Callable, Coroutine, Any, TypeVar, Generic, Optional, Tuple, Mapping, Union, Type
from typing_extensions import Protocol
from enum import Enum

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

class FixedStr(Parser[str]):
    fixed_string: str

    def __init__(self, fixed_string: str) -> None:
        self.fixed_string = fixed_string.lower()

    def parse(self, args: List[str]) -> ParseResult[str]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        if args[0].lower() == self.fixed_string.lower():
            return ParseOK(args[0], processed=1)
        else:
            return ParseFail("Expected {self.fixed_string}")

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
            return ParseOK(int(result.value), processed=result.processed)
        else:
            assert isinstance(result, ParseFail)
            return result

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

class Adjacent(Parser[Tuple[T1, T2]]):
    """Validates two values in the same order as the given validtors"""

    parser_left: Parser[T1]
    parser_right: Parser[T2]

    def __init__(self,
                 parser_left: Parser[T1],
                 parser_right: Parser[T2]) -> None:
        self.parser_left = parser_left
        self.parser_right = parser_right

    def parse(self, args: List[str]) -> ParseResult[Tuple[T1, T2]]:
        left = self.parser_left.parse(args)
        if isinstance(left, ParseFail):
            return ParseFail(f"{left.message} while parsing first argument")
        assert(isinstance(left, ParseOK))
        remaining = args[left.processed:]
        right = self.parser_right.parse(remaining)
        if isinstance(right, ParseFail):
            return ParseFail(f"{right.message} while parsing second argument")
        assert(isinstance(right, ParseOK))
        return ParseOK((left.value, right.value), processed=left.processed + right.processed)

class Seq(Parser[List[T]]):
    """Parser a sequence of values in the same order as the given parsers

    This requires all the validators to be of the same type. `Parser.any()` can be useful for
    achieving this, but you lose static type checking. Alternatively you can use `VldMap` to map
    the types.

    If you end up typing different types in the same list, you may find `Parser.base()` useful
    for the upcasting.
    """

    validators: List[Parser[T]]

    def __init__(self, validators: List[Parser[T]]) -> None:
        assert validators, "VldSeq: expected at least one parser"
        self.validators = validators

    def parse(self, args: List[str]) -> ParseResult[List[T]]:
        results: List[T] = []
        total_processed = 0
        for index, parser in enumerate(self.validators):
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
    validators: List[Parser[T]]

    def __init__(self, validators: List[Parser[T]]) -> None:
        self.validators = validators

    def parse(self, args: List[str]) -> ParseResult[T]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        for parser in self.validators:
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
    """Validates a sequence of values with given validators, but the order of the values can be anything
    and they can also be omitted in part or completely."""

    validators: List[Parser[T]]

    def __init__(self, validators: List[Parser[T]]) -> None:
        assert validators, "VldSeq: expected at least one parser"
        self.validators = validators

    def parse(self, args: List[str]) -> ParseResult[List[T]]:
        validators = self.validators
        total_processed = 0
        any_matched = True
        results: List[T] = []
        while any_matched:
            any_matched = False
            next_validators: List[Parser[T]] = []
            for parser in validators:
                result = parser.parse(args)
                if isinstance(result, ParseOK):
                    results.append(result.value)
                    total_processed += result.processed
                    args = args[result.processed:]
                    any_matched = True
                else:
                    next_validators.append(parser)
            validators = next_validators
        return ParseOK(results, processed=total_processed)

class HourMinute(Parser[Tuple[int, int]]):
    regex: Regex

    def __init__(self) -> None:
        self.regex = Regex(r"^([0-9]{1,2}):?([0-9]{2})$", [1, 2])

    def parse(self, args: List[str]) -> ParseResult[Tuple[int, int]]:
        if len(args) == 0:
            return ParseFail("No argument provided")
        result = self.regex.parse(args)
        if isinstance(result, ParseFail):
            return ParseFail("Failed to parse hh:mm")
        else:
            assert isinstance(result, ParseOK)
            hh, mm = (int(result.value[0]), int(result.value[1]))
            if hh > 23:
                return ParseFail("Hour cannot be >23")
            elif mm > 59:
                return ParseFail("Minute cannot be >59")
            else:
                return ParseOK((hh, mm), processed=result.processed)
