import re
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import List, Callable, Coroutine, Any, TypeVar, Generic, Optional, Tuple, Mapping, Union

Context = TypeVar("Context")
Validated = TypeVar("Validated")
T = TypeVar("T")
T1 = TypeVar("T1")
T2 = TypeVar("T2")
Tag = TypeVar('Tag')

class InvocationParseError(Exception):
    pass

@dataclass
class Invocation:
    name: str
    args: List[str]

    @staticmethod
    def parse(message: str) -> "Invocation":
        fields = re.split(r"  *", message)
        if len(fields):
            return Invocation(name=fields[0],
                              args=fields[1:])
        else:
            raise InvocationParseError()

class ValidatorResult(ABC, Generic[Validated]):
    def __str__(self) -> str:
        return self.__repr__()

    @abstractmethod
    def __repr__(self) -> str:
        pass

    def __eq__(self, other: object) -> bool:
        if isinstance(self, ValidatorOK) and isinstance(other, ValidatorOK):
            return (self.value, self.processed) == (other.value, other.processed)
        if isinstance(self, ValidatorFail) and isinstance(other, ValidatorFail):
            return self.message == other.message
        return False

class ValidatorOK(ValidatorResult[Validated]):
    value: Validated
    """Result of validation"""

    processed: int
    """How many args from invocation.args were processed"""

    def __init__(self, value: Validated, processed: int) -> None:
        self.value = value
        self.processed = processed

    def __repr__(self) -> str:
        value = str(self.value)
        if isinstance(self.value, str):
            value = f"\"{value}\""
        return f"ValidatorOK({value}, {self.processed})"

class ValidatorFail(ValidatorResult[Validated]):
    message: str
    def __init__(self, message: str):
        self.message = message

    def __repr__(self) -> str:
        return f"ValidatorFail(\"{self.message}\")"

class Validator(ABC, Generic[Validated]):
    def __call__(self, args: List[str]) -> ValidatorResult[Validated]:
        return self.validate(args)

    @abstractmethod
    def validate(self, args: List[str]) -> ValidatorResult[Validated]:
        pass

    def base(self) -> "Validator[Validated]":
        return self

    def any(self) -> "Validator[Any]":
        return self

Empty = Tuple[()]

class VldEmpty(Validator[Empty]):
    def validate(self, args: List[str]) -> ValidatorResult[Empty]:
        if len(args) == 0:
            return ValidatorOK((), processed=0)
        else:
            return ValidatorFail("Expected no more arguments")

class VldAnyStr(Validator[str]):
    def validate(self, args: List[str]) -> ValidatorResult[str]:
        if len(args) == 0:
            return ValidatorFail("No argument provided")
        return ValidatorOK(args[0], processed=1)

class VldFixedStr(Validator[str]):
    fixed_string: str

    def __init__(self, fixed_string: str) -> None:
        self.fixed_string = fixed_string.lower()

    def validate(self, args: List[str]) -> ValidatorResult[str]:
        if len(args) == 0:
            return ValidatorFail("No argument provided")
        if args[0].lower() == self.fixed_string.lower():
            return ValidatorOK(args[0], processed=1)
        else:
            return ValidatorFail("Expected {self.fixed_string}")

class VldRegex(Validator[Tuple[str, ...]]):
    regex: "re.Pattern[str]"
    groups: List[Union[int, str]]

    def __init__(self, regex: str, groups: List[Union[int, str]]) -> None:
        self.regex = re.compile(regex)
        self.groups = groups

    def validate(self, args: List[str]) -> ValidatorResult[Tuple[str, ...]]:
        if len(args) == 0:
            return ValidatorFail("No argument provided")
        match = re.match(self.regex, args[0])
        if match:
            return ValidatorOK(match.group(*self.groups), processed=1)
        else:
            return ValidatorFail(f"Failed to match regex {self.regex}")

class VldBool(Validator[bool]):
    def validate(self, args: List[str]) -> ValidatorResult[bool]:
        if len(args) == 0:
            return ValidatorFail("No argument provided")
        value = args[0].lower()
        if ["on", "true", "1"].count(value):
            return ValidatorOK(True, processed=1)
        elif ["off", "false", "0"].count(value):
            return ValidatorOK(False, processed=1)
        else:
            return ValidatorFail(f"Invalid argument \"{args[0]}\" for boolean")

class VldOptional(Validator[Optional[T]]):
    validator: Validator[T]

    def __init__(self, validator: Validator[T]) -> None:
        self.validator = validator

    def validate(self, args: List[str]) -> ValidatorResult[Optional[T]]:
        result = self.validator.validate(args)
        if isinstance(result, ValidatorOK):
            return ValidatorOK(result.value, processed=result.processed)
        else:
            return ValidatorOK(None, processed=0)

class VldValidOrMissing(Validator[Optional[T]]):
    validator: Validator[T]

    def __init__(self, validator: Validator[T]) -> None:
        self.validator = validator

    def validate(self, args: List[str]) -> ValidatorResult[Optional[T]]:
        if len(args) == 0:
            return ValidatorOK(None, processed=0)
        else:
            result = self.validator.validate(args)
            if isinstance(result, ValidatorOK):
                return ValidatorOK(result.value, processed=result.processed)
            else:
                assert isinstance(result, ValidatorFail)
                return ValidatorFail(result.message)

class VldMap(Generic[T1, T2], Validator[T2]):
    validator: Validator[T1]
    map: List[Callable[[T1], T2]]

    def __init__(self, validator: Validator[T1], map: Callable[[T1], T2]) -> None:
        self.validator = validator
        self.map = [map]

    def validate(self, args: List[str]) -> ValidatorResult[T2]:
        result = self.validator.validate(args)
        if isinstance(result, ValidatorOK):
            return ValidatorOK(self.map[0](result.value), processed=result.processed)
        else:
            assert isinstance(result, ValidatorFail)
            return ValidatorFail(result.message)

class VldMapDict(VldMap[List[Tuple[Tag, T]], Mapping[Tag, T]]):
    validator: Validator[List[Tuple[Tag, T]]]

    def __init__(self, validator: Validator[List[Tuple[Tag, T]]]) -> None:
        def mapping(xs: List[Tuple[Tag, T]]) -> Mapping[Tag, T]:
            return dict(xs)
        super().__init__(validator, map=mapping)

class VldTag(VldMap[T, Tuple[Tag, T]]):
    """Maps the result so that it is preceded by the given tag (in a 2-tuple)

    Can be useful with VldSomeOf for identifying which values came back.
    """

    tag: Tag

    def __init__(self, tag: Tag, validator: Validator[T]) -> None:
        def mapping(x: T) -> Tuple[Tag, T]:
            return (tag, x)
        super().__init__(validator, map=mapping)
        self.tag = tag

class VldAdjacent(Validator[Tuple[T1, T2]]):
    """Validates two values in the same order as the given validtors"""

    validator_left: Validator[T1]
    validator_right: Validator[T2]

    def __init__(self,
                 validator_left: Validator[T1],
                 validator_right: Validator[T2]) -> None:
        self.validator_left = validator_left
        self.validator_right = validator_right

    def validate(self, args: List[str]) -> ValidatorResult[Tuple[T1, T2]]:
        left = self.validator_left.validate(args)
        if isinstance(left, ValidatorFail):
            return ValidatorFail(f"{left.message} while parsing first argument")
        assert(isinstance(left, ValidatorOK))
        remaining = args[left.processed:]
        right = self.validator_right.validate(remaining)
        if isinstance(right, ValidatorFail):
            return ValidatorFail(f"{right.message} while parsing second argument")
        assert(isinstance(right, ValidatorOK))
        return ValidatorOK((left.value, right.value), processed=left.processed + right.processed)

class VldSeq(Validator[List[T]]):
    """Validates a sequence of values in the same order as the given validators

    This requires all the validators to be of the same type. `Validator.any()` can be useful for
    achieving this, but you lose static type checking. Alternatively you can use `VldMap` to map
    the types.

    If you end up typing different types in the same list, you may find `Validator.base()` useful
    for the upcasting.
    """

    validators: List[Validator[T]]

    def __init__(self, validators: List[Validator[T]]) -> None:
        assert validators, "VldSeq: expected at least one validator"
        self.validators = validators

    def validate(self, args: List[str]) -> ValidatorResult[List[T]]:
        results: List[T] = []
        total_processed = 0
        for index, validator in enumerate(self.validators):
            result = validator.validate(args)
            if isinstance(result, ValidatorFail):
                return ValidatorFail(f"{result.message} while parsing argument {index + 1}")
            assert(isinstance(result, ValidatorOK))
            results.append(result.value)
            total_processed += result.processed
            args = args[result.processed:]
        assert results
        return ValidatorOK(results, processed=total_processed)

class VldOneOf(Validator[T]):
    validators: List[Validator[T]]

    def __init__(self, validators: List[Validator[T]]) -> None:
        self.validators = validators

    def validate(self, args: List[str]) -> ValidatorResult[T]:
        if len(args) == 0:
            return ValidatorFail("No argument provided")
        for validator in self.validators:
            result = validator.validate(args)
            if isinstance(result, ValidatorOK):
                return result
        return ValidatorFail(f"Invalid value")

class VldOneOfStrings(Validator[str]):
    strings: List[str]

    def __init__(self, strings: List[str]) -> None:
        self.strings = strings

    def validate(self, args: List[str]) -> ValidatorResult[str]:
        if len(args) == 0:
            return ValidatorFail("No argument provided")
        if [str.lower() for str in self.strings].count(args[0]):
            return ValidatorOK(args[0], processed=1)
        else:
            valid_values = ", ".join(self.strings)
            return ValidatorFail(f"Expected one of {valid_values}")

class VldDelayed(Validator[T]):
    mk_validator: List[Callable[[], Validator[T]]]

    def __init__(self, validator: Callable[[], Validator[T]]):
        self.validator = validator

    def validate(self, args: List[str]) -> ValidatorResult[T]:
        return self.validator().validate(args)

class VldSomeOf(Validator[List[T]]):
    """Validates a sequence of values with given validators, but the order of the values can be anything
    and they can also be omitted in part or completely."""

    validators: List[Validator[T]]

    def __init__(self, validators: List[Validator[T]]) -> None:
        assert validators, "VldSeq: expected at least one validator"
        self.validators = validators

    def validate(self, args: List[str]) -> ValidatorResult[List[T]]:
        validators = self.validators
        total_processed = 0
        any_matched = True
        results: List[T] = []
        while any_matched:
            any_matched = False
            next_validators: List[Validator[T]] = []
            for validator in validators:
                result = validator.validate(args)
                if isinstance(result, ValidatorOK):
                    results.append(result.value)
                    total_processed += result.processed
                    args = args[result.processed:]
                    any_matched = True
                else:
                    next_validators.append(validator)
            validators = next_validators
        return ValidatorOK(results, processed=total_processed)

class VldHourMinute(Validator[Tuple[int, int]]):
    regex: VldRegex

    def __init__(self) -> None:
        self.regex = VldRegex(r"^([0-9]{1,2}):([0-9]{2})$", [1, 2])

    def validate(self, args: List[str]) -> ValidatorResult[Tuple[int, int]]:
        if len(args) == 0:
            return ValidatorFail("No argument provided")
        result = self.regex.validate(args)
        if isinstance(result, ValidatorFail):
            return ValidatorFail("Failed to parse hh:mm")
        else:
            assert isinstance(result, ValidatorOK)
            hh, mm = (int(result.value[0]), int(result.value[1]))
            if hh >= 23:
                return ValidatorFail("Hour cannot be >23")
            elif mm >= 59:
                return ValidatorFail("Minute cannot be >59")
            else:
                return ValidatorOK((hh, mm), processed=result.processed)

class Command(ABC, Generic[Context]):
    name: str

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def invoke(self, context: Context, invocation: Invocation) -> None:
        """Run the command"""
        pass

    @abstractmethod
    def validate(self, context: Context, invocation: Invocation) -> bool:
        """Validate the command arguments without invoking it"""
        pass

class Function(Command[Context], Generic[Context, Validated]):
    validator: Validator[Validated]
    fn: List[Callable[[Context, Validated], Coroutine[Any, Any, None]]]

    def __init__(self, name: str,
                 validator: Validator[Validated],
                 fn: Callable[[Context, Validated], Coroutine[Any, Any, None]]) -> None:
        super().__init__(name)
        self.validator = validator
        self.fn = [fn]

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        validated = self.validator(invocation.args)
        assert isinstance(validated, ValidatorOK), f"Expected invocation {invocation} to be validated: {validated}"
        await self.fn[0](context, validated.value)

    def validate(self, context: Context, invocation: Invocation) -> bool:
        return self.validator(invocation.args) is not None

class Commands(Generic[Context]):
    _commands: List[Command[Context]]

    def __init__(self) -> None:
        self._commands = []

    def register(self, command: Command[Context]) -> None:
        self._commands.append(command)

    def has_command(self, name: str) -> bool:
        return bool([command for command in self._commands if command.name == name])

    def validate(self, context: Context, invocation: Invocation) -> bool:
        """Validate requires the matching command to exist"""
        assert self.has_command(invocation.name)
        return bool([command for command in self._commands if command.name == invocation.name])

    async def invoke(self, context: Context, invocation: Invocation) -> None:
        for command in self._commands:
            if command.name == invocation.name:
                await command.invoke(context, invocation)
