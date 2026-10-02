import asyncio
import collections.abc
import contextlib
from typing import (
    List,
    Optional,
    Tuple,
    Callable,
    Awaitable,
    Any,
    TypeVar,
    Dict,
    Union,
    cast,
    NewType,
    Set,
    AsyncIterator,
)
import re
import datetime
from configparser import ConfigParser
from dataclasses import dataclass
import json
from enum import Enum
import math
import time
import hashlib
from abc import ABC, abstractmethod

import teslapy
from urllib.error import HTTPError
from urllib3.exceptions import ProtocolError
from requests.exceptions import ConnectionError
from requests.exceptions import HTTPError as RequestsHTTPError
import requests.exceptions
from oauthlib.oauth2 import OAuth2Error

from .control import Control, ControlCallback, CommandContext, MessageContext, MessageSendError
from .commands import Invocation
from . import log
from .config import Config
from .state import State, StateElement
from . import commands
from . import parser as p
from .utils import (
    assert_some,
    indent,
    call_with_delay_info,
    coalesce,
    round_to_next_second,
    map_optional,
)
from .env import Env
from .locations import (
    Location,
    Locations,
    LocationArgs,
    LocationArgsParser,
    LocationCommandContextBase,
    LocationInfoCoords,
    LatLon,
)
from .asyncthread import to_async
from . import __version__
from .appscheduler import AppScheduler
from google.cloud import firestore  # type: ignore

logger = log.getLogger(__name__)

T = TypeVar("T")

DEFAULT_NEAR_THRESHOLD_KM = 0.5


class AppException(Exception):
    pass


class ArgException(AppException):
    pass


class VehicleException(AppException):
    pass


def plain_data(value: Any) -> Any:
    """Detach JSON-like SDK data without reconstructing dict subclasses/sessions."""
    if isinstance(value, collections.abc.Mapping):
        if any(not isinstance(key, str) for key in value):
            raise VehicleException(f"Non-string Tesla data keys: {value!r}")
        return {key: plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_data(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise VehicleException(f"Unsupported Tesla data value: {value!r} ({type(value).__name__})")


def data_section(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def number(value: Any) -> Optional[float]:
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def vehicle_display_name(vehicle: collections.abc.Mapping[str, Any]) -> str:
    name = vehicle.get("display_name")
    return name if isinstance(name, str) and name else "Unnamed vehicle"


@dataclass
class VehicleSnapshot:
    vehicle_id: str
    display_name: str
    observed_at: datetime.datetime
    battery_level: Optional[int]
    charging_state: Optional[str]
    charge_limit: Optional[int]
    charge_amps: Optional[int]
    climate_on: Optional[bool]
    defrost_mode: Optional[int]
    inside_temp: Optional[float]
    outside_temp: Optional[float]
    temperature_unit: str
    data: Dict[str, Any]  # Detached plain data, never a live SDK Vehicle/session.


@dataclass
class ActionResult:
    vehicle_id: str
    action: str
    requested_value: Union[bool, int, str]
    success: bool
    error: Optional[str] = None


VehicleName = NewType("VehicleName", str)


class ValidVehicle(p.Map[str, VehicleName]):
    app: "App"

    def __init__(self, app: "App") -> None:
        super().__init__(
            map=lambda x: VehicleName(x), parser=p.Delayed[str](self.make_validator)
        )
        self.app = app

    def make_validator(self) -> p.Parser[str]:
        # TODO: Cannot do async stuff here, so the cached version must do
        vehicles = self.app.cached_vehicle_list
        display_names = [name for vehicle in vehicles
                         if isinstance(name := vehicle.get("display_name"), str) and name]
        return p.OneOfStrings(display_names)


class LocationDetail(Enum):
    Full = "full"  # show precise location information
    Near = "near"  # show precise location is near some predefined location
    At = "at"  # show only if location is near some predefined location
    Nearest = "nearest"  # show distance to the nearest location


class ChargeOp(ABC):
    @abstractmethod
    def get_command(self) -> Tuple[str, Dict[str, Any]]: ...


class ChargeOpStart(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("START_CHARGE", {})


class ChargeOpStop(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("STOP_CHARGE", {})


class ChargeOpPortOpen(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHARGE_PORT_DOOR_OPEN", {})


class ChargeOpPortClose(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHARGE_PORT_DOOR_CLOSE", {})


class ChargeOpSetAmps(ChargeOp):
    amps: int

    def __init__(self, amps: int) -> None:
        self.amps = amps
        if amps < 0 or amps > 32:
            raise ArgException("Amps should be in range 0..32")

    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHARGING_AMPS", {"charging_amps": str(self.amps)})


class ChargeOpSetLimit(ChargeOp):
    percent: int

    def __init__(self, percent: int) -> None:
        self.percent = percent
        if percent < 0 or percent > 100:
            raise ArgException("Percentage should be in range 0..100")

    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("CHANGE_CHARGE_LIMIT", {"percent": str(self.percent)})


class ChargeOpSchedulingEnable(ChargeOp):
    minutes_past_midnight: int

    def __init__(self, minutes_past_midnight: int) -> None:
        self.minutes_past_midnight = minutes_past_midnight
        if minutes_past_midnight < 0 or minutes_past_midnight >= 24 * 60:
            raise ArgException("Scheduled time does not fall within the day")

    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return (
            "SCHEDULED_CHARGING",
            {"enable": True, "time": self.minutes_past_midnight},
        )


class ChargeOpSchedulingDisable(ChargeOp):
    def get_command(self) -> Tuple[str, Dict[str, Any]]:
        return ("SCHEDULED_CHARGING", {"enable": False, "time": None})


class AppState(StateElement):
    app: "App"

    def __init__(self, app: "App") -> None:
        self.app = app

    async def save(self, state: State) -> None:
        if not state.has_section("tesla"):
            state["tesla"] = {}
        state["tesla"]["location_detail"] = self.app.location_detail.value
        state["tesla"]["override_vehicles"] = ", ".join(self.app.override_vehicles_lc)

        # TODO: move this to Control
        if not state.has_section("control"):
            state["control"] = {}
        state["control"]["require_bang"] = str(self.app.control.require_bang)


ClimateArgs = Tuple[Tuple[bool, Optional[VehicleName]], Tuple[()]]


def valid_on_off_vehicle(app: "App") -> p.Parser[ClimateArgs]:
    return p.Adjacent(
        p.Adjacent(p.Bool(), p.ValidOrMissing(ValidVehicle(app))), p.Empty()
    )


InfoArgs = Tuple[Tuple[Optional[str], Optional[VehicleName]], Tuple[()]]


def valid_info(app: "App") -> p.Parser[InfoArgs]:
    return p.Adjacent(
        p.Adjacent(
            p.ValidOrMissing(p.CaptureFixedStr("delta")),
            p.ValidOrMissing(ValidVehicle(app)),
        ),
        p.Empty(),
    )


LockUnlockArgs = Tuple[Optional[VehicleName], Tuple[()]]


def valid_lock_unlock(app: "App") -> p.Parser[LockUnlockArgs]:
    return p.Adjacent(p.ValidOrMissing(ValidVehicle(app)), p.Empty())


ChargeArgs = Tuple[Tuple[ChargeOp, Optional[VehicleName]], Tuple[()]]


def valid_charge(app: "App") -> p.Parser[ChargeArgs]:
    return p.Adjacent(
        p.Adjacent(
            p.OneOf[ChargeOp](
                p.Map(parser=p.CaptureFixedStr("start"), map=lambda _: ChargeOpStart()),
                p.Map(parser=p.CaptureFixedStr("stop"), map=lambda _: ChargeOpStop()),
                p.Map(
                    parser=p.Seq(
                        [p.CaptureFixedStr("port"), p.CaptureFixedStr("open")]
                    ),
                    map=lambda _: ChargeOpPortOpen(),
                ),
                p.Map(
                    parser=p.Seq(
                        [p.CaptureFixedStr("port"), p.CaptureFixedStr("close")]
                    ),
                    map=lambda _: ChargeOpPortClose(),
                ),
                p.Map(parser=p.Keyword("amps", p.Int()), map=ChargeOpSetAmps),
                p.Map(parser=p.Keyword("limit", p.Int()), map=ChargeOpSetLimit),
                p.Map(
                    parser=p.Keyword("schedule", p.CaptureFixedStr("disable")),
                    map=lambda x: ChargeOpSchedulingDisable(),
                ),
                p.Map(
                    parser=p.Keyword("schedule", p.HhMm()),
                    map=lambda x: ChargeOpSchedulingEnable(x[0] * 60 + x[1]),
                ),
            ),
            p.ValidOrMissing(ValidVehicle(app)),
        ),
        p.Empty(),
    )


class HeaterObject(ABC):
    @abstractmethod
    def get_command(
        self, heater_level: "HeaterLevel"
    ) -> Tuple[str, Dict[str, Any]]: ...


class HeaterSeat(HeaterObject):
    seat: int

    def __init__(self, seat: int) -> None:
        self.seat = seat
        if seat < 1 or seat > 6:
            raise ArgException("Seat should be in range 1..6")

    def get_command(self, heater_level: "HeaterLevel") -> Tuple[str, Dict[str, Any]]:
        return (
            "REMOTE_SEAT_HEATER_REQUEST",
            {"heater": self.seat - 1, "level": heater_level.numeric()},
        )


class HeaterSteering(HeaterObject):
    def get_command(self, heater_level: "HeaterLevel") -> Tuple[str, Dict[str, Any]]:
        return ("REMOTE_STEERING_WHEEL_HEATER_REQUEST", {"on": heater_level.binary()})


class HeaterLevel(Enum):
    Off = "off"
    Low = "low"
    Medium = "medium"
    High = "high"

    def numeric(self) -> int:
        return {
            HeaterLevel.Off: 0,
            HeaterLevel.Low: 1,
            HeaterLevel.Medium: 2,
            HeaterLevel.High: 3,
        }[self]

    def binary(self) -> int:
        return {
            HeaterLevel.Off: False,
            HeaterLevel.Low: True,
            HeaterLevel.Medium: True,
            HeaterLevel.High: True,
        }[self]


HeaterArgs = Tuple[
    Tuple[Tuple[HeaterObject, HeaterLevel], Optional[VehicleName]], Tuple[()]
]


def valid_heater(app: "App") -> p.Parser[HeaterArgs]:
    return p.Adjacent(
        p.Adjacent(
            p.Adjacent(
                p.OneOf[HeaterObject](
                    p.Map(parser=p.Keyword("seat", p.Int()), map=HeaterSeat),
                    p.Map(
                        parser=p.CaptureFixedStr("steering"),
                        map=lambda _: HeaterSteering(),
                    ),
                ),
                p.OneOfEnumValue(HeaterLevel),
            ),
            p.ValidOrMissing(ValidVehicle(app)),
        ),
        p.Empty(),
    )


ShareArgs = Tuple[Tuple[str, Optional[VehicleName]], Tuple[()]]


def valid_share(app: "App") -> p.Parser[ShareArgs]:
    return p.Adjacent(
        p.Adjacent(p.Concat(), p.ValidOrMissing(ValidVehicle(app))), p.Empty()
    )


def cmd_adjacent(label: str, parser: p.Parser[T]) -> p.Parser[Tuple[str, T]]:
    return p.Labeled(
        label=label, parser=p.Adjacent(p.CaptureFixedStr(label), parser).base()
    )


SetArgs = Callable[[CommandContext], Awaitable[None]]


def SetArgsParser(app: "App") -> p.Parser[SetArgs]:
    return app._set_commands.parser()


def format_time(dt: datetime.datetime) -> str:
    return dt.strftime("%H:%M")


def format_hours(hours: float) -> str:
    h = math.floor(hours)
    m = math.floor((hours % 1.0) * 60.0)
    return f"{h}h{m}m"


def format_km(km: float) -> str:
    if km < 1:
        return f"{km * 1000:.0f} m"
    else:
        return f"{km:.2f} km"


def cache_load() -> Dict[str, Any]:
    cache: Dict[str, Any] = (
        firestore.Client().collection("tesla").document("cache").get().to_dict()
    )
    return cache


def cache_dump(cache: Dict[str, Any]) -> None:
    cache_doc = firestore.Client().collection("tesla").document("cache")
    cache_doc.set(cache)


def miles_to_km(miles: float) -> float:
    return miles * 1.609


def format_temperature(celsius: Optional[float], unit: str) -> str:
    if celsius is None:
        return "unknown"
    temperature = celsius * 1.8 + 32 if unit == "F" else celsius
    return f"{temperature:g}°{unit}"


class TeslaSession(teslapy.Tesla):
    def send(self, request: Any, **kwargs: Any) -> Any:
        # TeslaPy bypasses its timeout for SSO, and OAuth passes timeout=None.
        # Enforce the default at the HTTP boundary, including token refresh.
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


def is_transient_error(error: Exception) -> bool:
    # Requests wraps response-body ProtocolError in ChunkedEncodingError.
    if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError,
                          ConnectionError, ProtocolError)):
        return True
    if isinstance(error, RequestsHTTPError):
        return error.response is not None and (error.response.status_code in (408, 429) or 500 <= error.response.status_code < 600)
    return isinstance(error, HTTPError) and (error.code in (408, 429) or 500 <= error.code < 600)


class App(ControlCallback):
    control: Control
    config: Config
    state: State
    tesla: teslapy.Tesla
    _commands: commands.Commands[CommandContext]
    _set_commands: commands.Commands[CommandContext]
    _scheduler: AppScheduler[None]
    locations: Locations
    location_detail: LocationDetail
    cached_vehicle_list: List[Dict[str, Any]]
    _prev_info: Dict[str, str]
    override_vehicles_lc: Set[str]  # If empty, query for devices

    def __init__(self, control: Control, env: Env) -> None:
        self.control = control
        self.config = env.config
        self.state = env.state
        self.locations = Locations(self.state)
        self.location_detail = LocationDetail.Full
        self.cached_vehicle_list = []
        self.override_vehicles_lc = {
            x
            for x in {
                x.lower().strip()
                for x in self.config.get(
                    "tesla", "override_vehicles", fallback="", empty_is_none=False
                ).split(",")
            }
            if x != ""
        }
        self._prev_info = {}
        self._operation_lock = asyncio.Lock()
        self._operation_owner: Optional[asyncio.Task[Any]] = None
        self._auth_lock = asyncio.Lock()
        self.auth_generation = 0
        self.auth_events: List[asyncio.Event] = []
        self._auth_transition = False
        control.callback = self
        cache_loader: Union[Callable[[], Dict[str, Any]], None] = None
        cache_dumper: Union[Callable[[Dict[str, Any]], None], None] = None
        if self.config.get("common", "storage") == "cloud":
            cache_loader = cache_load
            cache_dumper = cache_dump
        cache_file = self.config.get(
            "tesla", "credentials_store", fallback="cache.json"
        )
        def no_implicit_auth(url: str) -> str:
            raise AppException("Tesla authorization required; use the admin chat")

        self.tesla = TeslaSession(
            self.config.get("tesla", "email"),
            cache_file=cache_file,
            cache_dumper=cache_dumper,
            cache_loader=cache_loader,
            timeout=30,
            authenticator=no_implicit_auth,
        )
        self.authorized = bool(self.tesla.authorized)
        c = commands
        self._scheduler = AppScheduler(
            state=self.state,
            control=self.control,
            schedulable_commands=[
                cmd_adjacent("climate", valid_on_off_vehicle(self)).any(),
                cmd_adjacent("ac", valid_on_off_vehicle(self)).any(),
                cmd_adjacent("sauna", valid_on_off_vehicle(self)).any(),
                cmd_adjacent("info", valid_info(self)).any(),
                cmd_adjacent("lock", valid_lock_unlock(self)).any(),
                cmd_adjacent("unlock", valid_lock_unlock(self)).any(),
                cmd_adjacent("charge", valid_charge(self)).any(),
                cmd_adjacent("heater", valid_heater(self)).any(),
                cmd_adjacent("share", valid_share(self)).any(),
            ],
        )
        self._commands = c.Commands()
        self._scheduler.register(self._commands)
        self._commands.register(
            c.Function(
                "authorize",
                "Pass the Tesla API authorization URL",
                p.ValidOrMissing(p.Url()),
                self._command_authorized,
            )
        )
        self._commands.register(
            c.Function("vehicles", "List vehicles", p.Empty(), self._command_vehicles)
        )
        self._commands.register(
            c.Function(
                "climate",
                "climate on|off [vehicle] - control climate",
                valid_on_off_vehicle(self),
                self._command_climate,
            )
        )
        self._commands.register(
            c.Function(
                "ac",
                "ac on|off [vehicle] - same as climate",
                valid_on_off_vehicle(self),
                self._command_climate,
            )
        )
        self._commands.register(
            c.Function(
                "sauna",
                "sauna on|off [vehicle] - max defrost on/off",
                valid_on_off_vehicle(self),
                self._command_sauna,
            )
        )
        self._commands.register(
            c.Function(
                "info",
                "info [delta] [vehicle] - Show vehicle location, temperature, etc, or only difference (delta) to previous output",
                valid_info(self),
                self._command_info,
            )
        )
        self._commands.register(
            c.Function(
                "lock",
                "lock [vehicle] - Lock vehicle doors",
                valid_lock_unlock(self),
                self._command_lock,
            )
        )
        self._commands.register(
            c.Function(
                "unlock",
                "unlock [vehicle] - Unlock vehicle doors",
                valid_lock_unlock(self),
                self._command_unlock,
            )
        )
        self._commands.register(
            c.Function(
                "charge",
                "charge (start|stop|amps nnn|limit nnn|port (open|close)|schedule (hh:mm|disable)) [vehicle] - Manage charging and charging port",
                valid_charge(self),
                self._command_charge,
            )
        )
        self._commands.register(
            c.Function(
                "heater",
                "heater (seat (1..6)|steering) (off|low|medium|high) [vehicle] - Adjust seat and steering wheel heaters. Steering wheel heater can only be off or high.",
                valid_heater(self),
                self._command_heater,
            )
        )
        self._commands.register(
            c.Function(
                "share",
                "Share an address on an URL with the vehicle",
                valid_share(self),
                self._command_share,
            )
        )
        self._commands.register(
            c.Function(
                "location",
                f"location add|rm|ls\n{indent(2, self.locations.help())}",
                p.Remaining(LocationArgsParser(self.locations)),
                self._command_location,
            )
        )
        self._commands.register(
            c.Function("help", "Show help", p.Empty(), self._command_help)
        )
        self._commands.register(
            c.Function(
                "logout",
                "Log out from current user - authenticate again with !authorize",
                p.Empty(),
                self._command_logout,
            )
        )

        self._set_commands = c.Commands()
        self._set_commands.register(
            c.Function(
                "location-detail",
                "full, near, at, nearest",
                p.OneOfEnumValue(LocationDetail),
                self._command_set_location_detail,
            )
        )
        # TODO: move this to Control
        self._set_commands.register(
            c.Function(
                "require-!",
                "true or false, whether to require ! in front of commands",
                p.Remaining(p.Bool()),
                self._command_set_require_bang,
            )
        )

        self._set_commands.register(
            c.Function(
                "override-vehicles",
                "List of devices to interact with (limited from the list returned by the API)",
                p.Remaining(p.List_(p.AnyStr())),
                self._command_set_override_vehicles,
            )
        )

        self._commands.register(
            c.Function(
                "set",
                f"Set a configuration parameter\n{indent(2, self._set_commands.help())}",
                SetArgsParser(self),
                self._command_set,
            )
        )

    async def _command_help(
        self, command_context: CommandContext, args: Tuple[()]
    ) -> None:
        await self.control.send_message(
            command_context.to_message_context(), self._commands.help()
        )

    async def _command_logout(self, context: CommandContext, args: Tuple[()]) -> None:
        if not context.admin_room:
            await self.control.send_message(
                context.to_message_context(),
                "Please use the admin room for this command.",
            )
        else:
            async with self._auth_lock:
                # Close admission before draining an already sent request. Even
                # failed logout stays fail-closed until explicit authorization.
                self._auth_transition = True
                self._auth_changed(False)
                try:
                    async with self._operation(allow_transition=True):
                        had_credentials = bool(self.tesla.authorized)
                        await to_async(self.tesla.logout)
                finally:
                    self._auth_transition = False
            await self.control.send_message(
                context.to_message_context(), "Logout successful!" if had_credentials else "There is no user authorized! Please use !authorize."
            )

    async def command_callback(
        self, command_context: CommandContext, invocation: Invocation
    ) -> None:
        """ControlCallback"""
        logger.debug("command_callback(%s)", invocation.name)
        if self._commands.has_command(invocation.name):
            try:
                await self._commands.invoke(command_context, invocation)
            except (MessageSendError, asyncio.TimeoutError):
                raise
            except (AppException, teslapy.VehicleError, RequestsHTTPError, HTTPError,
                    requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError,
                    ConnectionError, ProtocolError, OAuth2Error) as exn:
                logger.exception("%s: Application command %s failed: %s", command_context.txn, invocation.name, exn)
                await self.control.send_message(
                    command_context.to_message_context(), str(exn) if isinstance(exn, AppException) else "Tesla request failed; please retry"
                )
            except commands.CommandsException as exn:
                raise exn
            except Exception as exn:
                logger.exception("%s: Application command %s failed: %s", command_context.txn, invocation.name, exn)
                raise
        else:
            await self.control.send_message(
                command_context.to_message_context(), "No such command"
            )

    async def _command_location(
        self, context: CommandContext, args: LocationArgs
    ) -> None:
        class LocationCommandContext(LocationCommandContextBase):
            app: App

            def __init__(self, app: App, context: CommandContext) -> None:
                super().__init__(context=context)
                self.app = app

            async def get_location(
                self, vehicle_name: Optional[str]
            ) -> Optional[LatLon]:
                def call(vehicle: teslapy.Vehicle) -> Any:
                    return plain_data(vehicle.get_vehicle_data())

                data = await self.app._command_on_vehicle(
                    context, vehicle_name, call, show_success=False
                )
                if data:
                    drive = data_section(data, "drive_state")
                    lat, lon = number(drive.get("latitude")), number(drive.get("longitude"))
                    if lat is not None and lon is not None:
                        return LatLon(lat, lon)
                    await self.app.control.send_message(context.to_message_context(), "Vehicle location is unavailable")
                    return None
                else:
                    return None

        await self.locations.command(LocationCommandContext(self, context), args)

    async def _command_share(self, context: CommandContext, args: ShareArgs) -> None:
        (url_or_address, vehicle_name), _ = args
        command = "SEND_TO_VEHICLE"
        logger.debug(f"Sending {command}")

        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(
                command,
                type="share_ext_content_raw",
                # locale="en-US",
                locale="fi",  # https://www.andiamo.co.uk/resources/iso-language-codes/
                timestamp_ms=int(time.time()),
                value={"android.intent.extra.TEXT": url_or_address},
            )

        await self._command_on_vehicle(context, vehicle_name, call)
        pass

    async def _command_set(self, context: CommandContext, args: SetArgs) -> None:
        await args(context)

    async def _command_set_location_detail(
        self, context: CommandContext, args: LocationDetail
    ) -> None:
        self.location_detail = args
        await self.state.save()
        await self.control.send_message(
            context.to_message_context(),
            f"Location detail set to {self.location_detail.value}",
        )

    # TODO: move this to Control
    async def _command_set_require_bang(
        self, context: CommandContext, args: bool
    ) -> None:
        self.control.require_bang = args
        await self.state.save()
        await self.control.send_message(
            context.to_message_context(),
            f"Require bang set to {self.control.require_bang}",
        )

    async def _command_set_override_vehicles(
        self, context: CommandContext, args: List[str]
    ) -> None:
        self.override_vehicles_lc = {arg.lower() for arg in args}
        self._auth_changed(self.authorized)
        if self.authorized:
            await self._get_vehicle_list()
        await self.state.save()
        await self.control.send_message(
            context.to_message_context(),
            f"Override vehicles set to {self.override_vehicles_lc}",
        )

    async def _command_authorized(
        self, context: CommandContext, authorization_response: Optional[str]
    ) -> None:
        if self.authorized:
            await self.control.send_message(
                context.to_message_context(), "Already authorized!"
            )
        elif not context.admin_room:
            await self.control.send_message(
                context.to_message_context(),
                "Please use the admin room for this command.",
            )
        else:
            if authorization_response is not None:
                # https://github.com/python/mypy/issues/9590
                def call() -> None:
                    if self.tesla.authorized:
                        self.tesla.logout()
                    self.tesla.fetch_token(
                        authorization_response=authorization_response
                    )

                async with self._auth_lock:
                    if self.authorized:
                        raise AppException("Already authorized!")
                    async with self._operation(allow_transition=True):
                        try:
                            await to_async(call)
                        except asyncio.CancelledError:
                            self._auth_changed(bool(self.tesla.authorized))
                            raise
                        except Exception:
                            self._auth_changed(False)
                            raise
                        self._auth_changed(bool(self.tesla.authorized))
                        # Cache initialization is independent of auth commit and
                        # broker readiness. A failed read leaves named parsing empty.
                        try:
                            await self._get_vehicle_list()
                        except Exception:
                            logger.warning("Authorized vehicle enumeration unavailable", exc_info=True)
                await self.control.send_message(
                    context.to_message_context(), "Authorization successful" if self.authorized else "Authorization unavailable; please authorize again"
                )
            elif not self.authorized:
                generation = self.auth_generation
                authorization_url = await self._authorization_url(generation)
                if authorization_url is not None and generation == self.auth_generation and not self.authorized:
                    await self.control.send_message(
                        context.to_message_context(),
                        f'Not authorized. Authorization URL: {authorization_url} "Page Not Found" will be shown at success. Use !authorize https://the/url/you/ended/up/at',
                    )
                else:
                    await self.control.send_message(context.to_message_context(),
                                                    "Already authorized!" if self.authorized else "Authorization changed; please retry !authorize")

    async def _authorization_url(self, generation: int) -> Optional[str]:
        async with self._auth_lock:
            if generation != self.auth_generation or self.authorized:
                return None
            async with self._operation(allow_transition=True):
                if generation != self.auth_generation or self.authorized:
                    return None
                def call() -> str:
                    # App may have rejected a token the SDK still considers valid.
                    if self.tesla.authorized:
                        self.tesla.logout()
                    url = self.tesla.authorization_url()
                    if not url:
                        raise AppException("Tesla did not provide an authorization URL")
                    return url
                try:
                    url = await to_async(call)
                except (OSError, OAuth2Error, AppException, ProtocolError) as exn:
                    logger.warning("Authorization initialization failed: %s", exn, exc_info=True)
                    raise AppException("Unable to start authorization; check credential storage/connectivity and retry !authorize") from exn
                return url if generation == self.auth_generation and not self.authorized else None

    async def _get_vehicle_list(self, sdk_objects: bool = False) -> List[Any]:
        if sdk_objects and self._operation_owner is not asyncio.current_task():
            raise AppException("SDK vehicle selection requires the Tesla operation gate")
        if not self.authorized:
            raise AppException("Tesla authorization required")
        generation = self.auth_generation
        def call() -> Tuple[List[Any], List[Dict[str, Any]]]:
            vehicle_list = self.tesla.vehicle_list()
            if self.override_vehicles_lc != set():
                vehicle_list = [
                    vehicle
                    for vehicle in vehicle_list
                    if isinstance(vehicle.get("display_name"), str)
                    and vehicle.get("display_name").lower() in self.override_vehicles_lc
                ]
            return vehicle_list, [plain_data(vehicle) for vehicle in vehicle_list]

        result_or_error = await self._retry_to_async(call)
        if isinstance(result_or_error, Exception):
            raise result_or_error
        assert result_or_error is not None
        if generation != self.auth_generation:
            raise AppException("Authorization changed; enumeration discarded")
        vehicles, metadata = result_or_error
        self.cached_vehicle_list = metadata
        return vehicles if sdk_objects else metadata

    async def _command_vehicles(
        self, context: CommandContext, valid: Tuple[()]
    ) -> None:
        vehicles = await self._get_vehicle_list()
        await self.control.send_message(
            context.to_message_context(), f"vehicles: {vehicles}"
        )

    async def _get_vehicle(self, display_name: Optional[str]) -> teslapy.Vehicle:
        vehicles = await self._get_vehicle_list(sdk_objects=True)
        if display_name is not None:
            vehicles = [
                vehicle
                for vehicle in vehicles
                if isinstance(vehicle.get("display_name"), str)
                and vehicle.get("display_name").lower() == display_name.lower()
            ]
        if len(vehicles) > 1:
            raise ArgException("Matched more than one vehicle; aborting")
        elif len(vehicles) == 0:
            if display_name is not None:
                raise ArgException(f"No vehicle found by name {display_name}")
            else:
                raise ArgException(f"No vehicle found")
        else:
            logger.debug("vehicle=%s", vehicles[0])
            return vehicles[0]

    async def _get_vehicle_by_id(self, vehicle_id: str) -> teslapy.Vehicle:
        vehicles = [v for v in await self._get_vehicle_list(sdk_objects=True) if self._vehicle_id(v) == vehicle_id]
        if len(vehicles) != 1:
            raise ArgException("Vehicle identity not found or ambiguous")
        return vehicles[0]

    async def _wake(self, context: Optional[CommandContext], vehicle: teslapy.Vehicle) -> None:
        for key in ("display_name", "state", "id_s"):
            if not isinstance(vehicle.get(key), str) or not vehicle.get(key):
                raise VehicleException(f"Vehicle metadata missing {key}: {vehicle}")
        async def report() -> None:
            if context is not None:
                await self.control.send_message(
                    context.to_message_context(), f"Waking up {vehicle_display_name(vehicle)}"
                )

        try:
            await call_with_delay_info(
                delay_sec=5.0, report=report, task=self._retry_to_async(vehicle.sync_wake_up)
            )
        except teslapy.VehicleError as exn:
            raise VehicleException(f"Failed to wake up vehicle: {exn}; aborting") from exn

    async def _load_state(self) -> None:
        if self.state.has_section("tesla"):
            if self.state["tesla"].has_key("override_vehicles"):
                self.override_vehicles_lc = {name.strip().lower() for name in self.state["tesla"]["override_vehicles"].split(",") if name.strip()}
            location_detail_value = self.state.get(
                "tesla", "location_detail", fallback=LocationDetail.Full.value
            )
            matching_location_details = [
                enum
                for enum in LocationDetail.__members__.values()
                if enum.value == location_detail_value
            ]
            self.location_detail = matching_location_details[0]

        # TODO: move this to Control
        if self.state.has_section("control"):
            self.control.require_bang = bool(
                self.state.get(
                    "control", "require_bang", fallback=str(self.control.require_bang)
                )
                == str(True)
            )

    def format_location(self, location: Location) -> str:
        nearest_name, nearest = self.locations.nearest_location(location)
        near_threshold = coalesce(location.near_km, DEFAULT_NEAR_THRESHOLD_KM)
        # TODO: but there could be another location that's not the nearest, but has
        # a larger near_km..
        distance = location.km_to(nearest) if nearest is not None else None
        near = nearest and (
            distance < near_threshold if distance is not None else False
        )
        # just so we need to check less stuff in the code..
        distance_str = f"{format_km(distance)}" if distance is not None else ""
        if self.location_detail == LocationDetail.Full:
            # show precise location information
            st = f"{location} {location.url()}"
            if nearest_name is not None:
                st += f" {distance_str} to {nearest_name}"
            return st
        else:
            if self.location_detail == LocationDetail.Near:
                # show precise location is near some predefined location
                if near:
                    return (
                        f"{location} {location.url()} {distance_str} to {nearest_name}"
                    )
                else:
                    return f""
            elif self.location_detail == LocationDetail.At:
                # show only if location is near some predefined location
                if near:
                    return f"{distance_str} to {nearest_name}"
                else:
                    return ""
            elif self.location_detail == LocationDetail.Nearest:
                # show distance to the nearest location
                if nearest:
                    return f"{distance_str} to {nearest_name}"
                else:
                    return f""
            else:
                assert False

    async def refresh_vehicle(
        self, vehicle_name: Optional[str], context: Optional[CommandContext] = None,
        vehicle_id: Optional[str] = None,
    ) -> VehicleSnapshot:
        def call(vehicle: teslapy.Vehicle) -> Any:
            return plain_data(vehicle.get_vehicle_data())

        vehicle, data = await self._execute_vehicle(vehicle_name, call, context, vehicle_id)
        climate = data_section(data, "climate_state")
        charge = data_section(data, "charge_state")
        def integer(value: Any) -> Optional[int]:
            return value if type(value) is int else None
        climate_on = climate.get("is_climate_on", climate.get("is_auto_conditioning_on"))
        return VehicleSnapshot(
            vehicle_id=self._vehicle_id(vehicle),
            display_name=vehicle_display_name(vehicle),
            observed_at=datetime.datetime.now(datetime.timezone.utc),
            battery_level=integer(charge.get("battery_level")),
            charging_state=charge.get("charging_state") if isinstance(charge.get("charging_state"), str) else None,
            charge_limit=integer(charge.get("charge_limit_soc")),
            charge_amps=integer(charge.get("charge_current_request")),
            climate_on=climate_on if type(climate_on) is bool else None,
            defrost_mode=integer(climate.get("defrost_mode")),
            inside_temp=number(climate.get("inside_temp")),
            outside_temp=number(climate.get("outside_temp")),
            temperature_unit="C",  # Tesla API temperatures are Celsius regardless of GUI setting.
            data=data,
        )

    async def _perform_action(
        self, vehicle_name: Optional[str], action: str,
        requested_value: Union[bool, int], command: str,
        kwargs: Optional[Dict[str, Any]] = None,
        context: Optional[CommandContext] = None,
        vehicle_id: Optional[str] = None,
    ) -> ActionResult:
        identity = vehicle_id or ""
        def selected(vehicle: teslapy.Vehicle) -> None:
            nonlocal identity
            identity = self._vehicle_id(vehicle)
        try:
            def call(vehicle: teslapy.Vehicle) -> Any:
                return vehicle.command(command, **(kwargs or {}))

            vehicle, result = await self._execute_vehicle(vehicle_name, call, context, vehicle_id, on_selected=selected)
            if result is False or (isinstance(result, dict) and result.get("result") is False):
                return ActionResult(self._vehicle_id(vehicle), action,
                                    requested_value, False, str(result.get("reason") or "Vehicle rejected command") if isinstance(result, dict) else "Vehicle rejected command")
            return ActionResult(self._vehicle_id(vehicle), action,
                                requested_value, True)
        except (AppException, teslapy.VehicleError, HTTPError, RequestsHTTPError, ProtocolError,
                ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError, OAuth2Error) as exn:
            logger.exception("Vehicle action %s for %s failed: requested %s", action, identity or vehicle_name, requested_value)
            return ActionResult(identity, action, requested_value, False, str(exn) or type(exn).__name__)

    async def set_ac(
        self, vehicle_name: Optional[str], enabled: bool,
        context: Optional[CommandContext] = None,
        vehicle_id: Optional[str] = None,
    ) -> ActionResult:
        if type(enabled) is not bool:
            raise ArgException("AC state must be a boolean")
        return await self._perform_action(vehicle_name, "ac", enabled,
                                          "CLIMATE_ON" if enabled else "CLIMATE_OFF", context=context,
                                          vehicle_id=vehicle_id)

    async def set_sauna(
        self, vehicle_name: Optional[str], enabled: bool,
        context: Optional[CommandContext] = None,
        vehicle_id: Optional[str] = None,
    ) -> ActionResult:
        if type(enabled) is not bool:
            raise ArgException("Sauna state must be a boolean")
        return await self._perform_action(vehicle_name, "sauna", enabled,
                                          "MAX_DEFROST", {"on": enabled}, context, vehicle_id)

    async def set_charge_limit(
        self, vehicle_name: Optional[str], percent: int,
        context: Optional[CommandContext] = None,
        vehicle_id: Optional[str] = None,
    ) -> ActionResult:
        if type(percent) is not int:
            raise ArgException("Charge limit must be an integer")
        op = ChargeOpSetLimit(percent)
        command, kwargs = op.get_command()
        return await self._perform_action(vehicle_name, "charge_limit", percent,
                                          command, kwargs, context, vehicle_id)

    async def _command_info(self, context: CommandContext, args: InfoArgs) -> None:
        (delta_kwd, vehicle_name), _ = args
        delta_mode = delta_kwd == "delta" and not context.scheduled
        try:
            snapshot = await self.refresh_vehicle(vehicle_name, context)
            data = snapshot.data
            logger.debug("data: %s", data)
            def required(section: str, key: str, kinds: Tuple[type, ...], nullable: bool = False) -> Any:
                values = data_section(data, section)
                value = values.get(key)
                if key not in values or (value is None and not nullable):
                    raise VehicleException(f"Vehicle information unavailable: {section}.{key}")
                if value is not None and (type(value) not in kinds or
                                         (type(value) in (int, float) and number(value) is None)):
                    raise VehicleException(f"Invalid vehicle information {section}.{key}: {value!r}")
                return value
            dist_hr_unit = required("gui_settings", "gui_distance_units", (str,))
            dist_unit = assert_some(
                re.match(r"^[^/]*", dist_hr_unit),
                "Expected to find / from dist_hr_unit",
            )[0]
            temp_unit = required("gui_settings", "gui_temperature_units", (str,))
            if temp_unit not in ("C", "F") or dist_unit not in ("km", "mi"):
                raise VehicleException(f"Unsupported vehicle units: {dist_hr_unit!r}, {temp_unit!r}")
            drive_state = data_section(data, "drive_state")
            gps_as_of = drive_state.get("gps_as_of")
            heading = number(drive_state.get("heading"))
            lat = number(drive_state.get("latitude"))
            lon = number(drive_state.get("longitude"))
            speed = number(drive_state.get("speed"))
            battery_level = required("charge_state", "battery_level", (int, float))
            battery_range = required("charge_state", "battery_range", (int, float))
            est_battery_range = required("charge_state", "est_battery_range", (int, float))
            charge_limit = required("charge_state", "charge_limit_soc", (int, float))
            charge_current_request = required("charge_state", "charge_current_request", (int, float))
            scheduled_charging_mode = required("charge_state", "scheduled_charging_mode", (str,))
            scheduled_charging_start_time = required("charge_state", "scheduled_charging_start_time", (int, float), nullable=True)
            charge_rate = required("charge_state", "charge_rate", (int, float))
            charging_state = required("charge_state", "charging_state", (str,))
            time_to_full_charge = required("charge_state", "time_to_full_charge", (int, float))
            car_version = required("vehicle_state", "car_version", (str,))
            front_trunk_open = required("vehicle_state", "ft", (int, float)) != 0
            rear_trunk_open = required("vehicle_state", "rt", (int, float)) != 0
            locked = required("vehicle_state", "locked", (bool,))
            front_driver_window = required("vehicle_state", "fd_window", (int, float)) != 0
            front_passanger_window = required("vehicle_state", "fp_window", (int, float)) != 0
            rear_driver_window = required("vehicle_state", "rd_window", (int, float)) != 0
            rear_passanger_window = required("vehicle_state", "rp_window", (int, float)) != 0
            valet_mode = required("vehicle_state", "valet_mode", (bool,))
            odometer = int(required("vehicle_state", "odometer", (int, float)))
            display_name = required("vehicle_state", "vehicle_name", (str,))
            climate_state = data_section(data, "climate_state")
            inside_temp = number(climate_state.get("inside_temp"))
            outside_temp = number(climate_state.get("outside_temp"))
            climate_on = climate_state.get(
                "is_climate_on", climate_state.get("is_auto_conditioning_on")
            )
            if type(climate_on) is not bool:
                climate_on = None
            preconditioning = climate_state.get("is_preconditioning") is True
            climate_keeper_mode = climate_state.get("climate_keeper_mode")
            if not isinstance(climate_keeper_mode, str):
                climate_keeper_mode = None
            driver_temp_setting = number(climate_state.get("driver_temp_setting"))
            passenger_temp_setting = number(climate_state.get("passenger_temp_setting"))
            seat_heater_left = climate_state.get("seat_heater_left")
            seat_heater_right = climate_state.get("seat_heater_right")
            seat_heater_rear_center = climate_state.get("seat_heater_rear_center")
            seat_heater_rear_left = climate_state.get("seat_heater_rear_left")
            seat_heater_rear_right = climate_state.get("seat_heater_rear_right")

            message = ""
            last_topic = ""
            buffer = ""
            pending_info: Dict[str, str] = {}
            # Scheduled output is full and never advances an interactive
            # destination's presentation history.
            destination = (id(context.to_message_context().origin), context.admin_room)

            def track(topic: str, contents: str) -> None:
                """Once topic changes, check if its contents changed since the previous round

                Always keeps track, but filters unchanged fields only if in delta mode.
                """
                nonlocal buffer
                nonlocal message
                nonlocal last_topic

                if topic != last_topic:
                    topic_key = f"{destination}:{snapshot.vehicle_id}:{last_topic}"
                    if self._prev_info.get(topic_key, "") != buffer:
                        message += buffer
                        pending_info[topic_key] = buffer
                    elif not delta_mode:
                        message += buffer
                    buffer = ""
                    last_topic = topic
                buffer += contents

            track("version", f"{display_name} version {car_version}\n")
            seat_heaters_str = ", ".join(
                [
                    str(x) if type(x) is int else "unknown"
                    for x in [
                        seat_heater_left,
                        seat_heater_right,
                        seat_heater_rear_left,
                        seat_heater_rear_center,
                        seat_heater_rear_right,
                    ]
                ]
            )
            track(
                "temperature",
                f"Inside: {format_temperature(inside_temp, temp_unit)} Outside: {format_temperature(outside_temp, temp_unit)} Seat heaters: {seat_heaters_str}\n",
            )
            if preconditioning:
                climate_status = "preconditioning"
            elif climate_on is None:
                climate_status = "unknown"
            else:
                climate_status = "on" if climate_on else "off"
            if climate_keeper_mode not in (None, "off"):
                climate_status += f" ({climate_keeper_mode})"
            if driver_temp_setting == passenger_temp_setting:
                target_temperature = (
                    format_temperature(driver_temp_setting, temp_unit)
                    if driver_temp_setting is not None
                    else None
                )
            else:
                target_temperature = " / ".join(
                    format_temperature(temp, temp_unit)
                    for temp in (driver_temp_setting, passenger_temp_setting)
                    if temp is not None
                ) or None
            track(
                "temperature",
                f"Climate: {climate_status}"
                + (f" Target: {target_temperature}" if target_temperature else "")
                + "\n",
            )
            track("location", f"Heading: {heading if heading is not None else 'unknown'}\n")
            track(
                "location",
                "Location: "
                + (
                    self.format_location(Location(lat=lat, lon=lon))
                    if lat is not None and lon is not None
                    else "unknown"
                )
                + "\n",
            )
            track("location", f"Speed: {speed if speed is not None else 'unknown'}\n")
            track(
                "battery",
                f"Battery: {battery_level}% {battery_range} {dist_unit} est. {est_battery_range} {dist_unit}\n",
            )
            charge_eta = datetime.datetime.now() + datetime.timedelta(
                hours=time_to_full_charge
            )
            track("battery", f"Charge limit: {charge_limit}%")
            track("battery", f" Charge current limit: {charge_current_request}A")
            if charge_rate or charging_state == "Charging":
                track("battery", f" Charge rate: {charge_rate}A")
                if time_to_full_charge > 0:
                    track(
                        "battery",
                        f" Ready at: {format_time(charge_eta)} (+{format_hours(time_to_full_charge)})",
                    )
                else:
                    track("battery", f" Ready at: unknown")
            if scheduled_charging_mode == "StartAt":
                track(
                    "battery",
                    "\nCharging scheduled to start at "
                    + str(
                        map_optional(
                            scheduled_charging_start_time,
                            lambda x: datetime.datetime.fromtimestamp(x).strftime(
                                "%Y-%m-%d %H:%M"
                            ),
                        )
                    ),
                )
            track(
                "odometer",
                f"\nOdometer: {miles_to_km(odometer) if dist_unit == 'km' else odometer} {dist_unit}",
            )
            track("lock", f"\nVehicle is {'locked' if locked else 'unlocked'}")
            if valet_mode:
                track("windows", f"\nValet mode enabled")
            if front_trunk_open:
                track("windows", f"\nFrunk open")
            if rear_trunk_open:
                track("windows", f"\nTrunk open")
            if front_driver_window:
                track("windows", f"\nFront driver window open")
            if front_passanger_window:
                track("windows", f"\nFront passanger window open")
            if rear_driver_window:
                track("windows", f"\nRear driver side window open")
            if rear_passanger_window:
                track("windows", f"\nRear passanger side window open")
            track("", "")
            if message == "":
                message = "Nothing changed"
            await self.control.send_message(
                context.to_message_context(), message.strip()
            )
            if not context.scheduled:
                self._prev_info.update(pending_info)
        except HTTPError as exn:
            await self.control.send_message(context.to_message_context(), str(exn))
        except (AppException, teslapy.VehicleError, ProtocolError, ConnectionError) as exn:
            logger.exception("Vehicle info request failed: vehicle %s", vehicle_name)
            await self.control.send_message(context.to_message_context(), f"Error: {exn}")

    async def _command_lock(
        self, context: CommandContext, args: LockUnlockArgs
    ) -> None:
        vehicle_name, _ = args
        command = "LOCK"
        logger.debug(f"Sending {command}")

        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command)

        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_unlock(
        self, context: CommandContext, args: LockUnlockArgs
    ) -> None:
        vehicle_name, _ = args
        command = "UNLOCK"
        logger.debug(f"Sending {command}")

        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command)

        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_charge(self, context: CommandContext, args: ChargeArgs) -> None:
        (charge_op, vehicle_name), _ = args
        if isinstance(charge_op, ChargeOpSetLimit):
            result = await self.set_charge_limit(vehicle_name, charge_op.percent, context)
            await self.control.send_message(context.to_message_context(),
                                            "Success!" if result.success else f"Error: {result.error}")
            return
        command, kwargs = charge_op.get_command()
        logger.debug(f"Sending {command} {kwargs}")

        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command, **kwargs)

        await self._command_on_vehicle(context, vehicle_name, call)

    async def _command_heater(self, context: CommandContext, args: HeaterArgs) -> None:
        ((heater_object, heater_level), vehicle_name), _ = args
        command, kwargs = heater_object.get_command(heater_level)
        logger.debug(f"Sending {command} {kwargs}")

        def call(vehicle: teslapy.Vehicle) -> Any:
            return vehicle.command(command, **kwargs)

        await self._command_on_vehicle(context, vehicle_name, call)

    async def _retry(self, fn: Callable[[], Awaitable[T]]) -> T:
        generation = self.auth_generation
        num_retries = 0
        result_is_set = False
        result: T
        error = None
        while num_retries < 15:
            if generation != self.auth_generation:
                raise AppException("Authorization changed; retry discarded")
            try:
                result = await fn()
                result_is_set = True
                error = None
                break
            except teslapy.VehicleError as exn:
                logger.debug("Vehicle error: %s", exn, exc_info=True)
                error = exn
                if exn.args[0] != "could_not_wake_buses":
                    break
            except HTTPError as exn:
                logger.debug("HTTP error: %s", exn, exc_info=True)
                error = exn
            except (RequestsHTTPError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as exn:
                if not is_transient_error(exn):
                    raise
                logger.debug("Transient Tesla request failed: %s", exn, exc_info=True)
                error = exn
            except ProtocolError as exn:
                logger.debug("HTTP protocol error: %s", exn, exc_info=True)
                error = exn
            except ConnectionError as exn:
                logger.debug("HTTP connection error: %s", exn, exc_info=True)
                error = exn
            finally:
                logger.debug(f"Retry round complete")
            await asyncio.sleep(5 + pow(1.15, num_retries) * 2)
            num_retries += 1
        if num_retries > 0:
            logger.debug(f"Number of retries: {num_retries}")
        if error is not None:
            raise error
        assert result_is_set
        return result

    async def _retry_to_async(self, fn: Callable[[], T]) -> T:
        async def call() -> T:
            def call2() -> T:
                return fn()

            return await to_async(call2)

        async with self._operation():
            try:
                return await self._retry(call)
            except (OAuth2Error, RequestsHTTPError) as exn:
                if isinstance(exn, OAuth2Error) or (exn.response is not None and exn.response.status_code in (401, 403)):
                    self._auth_changed(False)
                raise

    def _auth_changed(self, authorized: bool) -> None:
        self.authorized = authorized
        self.auth_generation += 1
        self.cached_vehicle_list = []
        for event in self.auth_events:
            event.set()
        logger.info("Tesla authorization state changed: %s generation %d", authorized, self.auth_generation)

    @contextlib.asynccontextmanager
    async def _operation(self, allow_transition: bool = False) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if self._operation_owner is task:
            yield
            return
        generation = self.auth_generation
        if self._auth_transition and not allow_transition:
            raise AppException("Authorization transition in progress")
        async with self._operation_lock:
            if not allow_transition and (generation != self.auth_generation or self._auth_transition):
                raise AppException("Authorization changed; retry the request")
            self._operation_owner = task
            try:
                yield
            finally:
                self._operation_owner = None

    async def _execute_vehicle(
        self, vehicle_name: Optional[str], fn: Callable[[teslapy.Vehicle], T],
        context: Optional[CommandContext] = None,
        vehicle_id: Optional[str] = None,
        on_selected: Optional[Callable[[teslapy.Vehicle], None]] = None,
    ) -> Tuple[Dict[str, Any], T]:
        generation = self.auth_generation
        async with self._operation():
            if not self.authorized:
                raise AppException("Tesla authorization required")
            vehicle = (await self._get_vehicle_by_id(vehicle_id) if vehicle_id is not None
                       else await self._get_vehicle(vehicle_name))
            if on_selected is not None:
                on_selected(vehicle)
            await self._wake(context, vehicle)
            if generation != self.auth_generation:
                raise AppException("Authorization changed; request discarded")
            def call() -> Tuple[Dict[str, Any], T]:
                result = fn(vehicle)
                return plain_data(vehicle), result
            metadata, result = await self._retry_to_async(call)
            if generation != self.auth_generation:
                raise AppException("Authorization changed; result discarded")
            return metadata, result

    @staticmethod
    def _vehicle_id(vehicle: collections.abc.Mapping[str, Any]) -> str:
        # Do not put the VIN (or arbitrary display names) into MQTT topics.
        identity = vehicle.get("vin") or vehicle.get("display_name")
        if not isinstance(identity, str) or not identity:
            raise VehicleException(f"Vehicle has no usable VIN or display name: {vehicle}")
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    async def _command_on_vehicle(
        self,
        context: CommandContext,
        vehicle_name: Optional[str],
        fn: Callable[[teslapy.Vehicle], T],
        show_success: bool = True,
    ) -> Optional[T]:
        result: Optional[T] = None
        try:
            _, result = await self._execute_vehicle(vehicle_name, fn, context)
        except AppException as exn:
            await self.control.send_message(
                context.to_message_context(), f"Error: {exn}"
            )
            return None
        except teslapy.VehicleError as exn:
            await self.control.send_message(
                context.to_message_context(), f"Error: {exn}"
            )
            return None
        except Exception as exn:
            logger.exception("%s: Vehicle operation failed: %s", context.txn, exn)
            await self.control.send_message(
                context.to_message_context(), f"{context.txn} Exception :("
            )
            return None
        if show_success:
            message = "Success!"
            if result != True:  # this never happens, though?
                message += f" {result}"
            await self.control.send_message(context.to_message_context(), message)
        return result

    async def _command_climate(
        self, context: CommandContext, args: ClimateArgs
    ) -> None:
        (mode, vehicle_name), _ = args
        result = await self.set_ac(vehicle_name, mode, context)
        await self.control.send_message(context.to_message_context(),
                                        "Success!" if result.success else f"Error: {result.error}")

    async def _command_sauna(self, context: CommandContext, args: ClimateArgs) -> None:
        (mode, vehicle_name), _ = args
        result = await self.set_sauna(vehicle_name, mode, context)
        await self.control.send_message(context.to_message_context(),
                                        "Success!" if result.success else f"Error: {result.error}")

    async def initialize(self) -> None:
        await self._load_state()
        self.state.add_element(AppState(self))
        if self.control.run_scheduled_commands:
            await self._scheduler.load()
        if self.authorized:
            try:
                await self._get_vehicle_list()
            except Exception:
                logger.warning("Startup vehicle enumeration unavailable", exc_info=True)

    async def run(self) -> None:
        if self.control.run_scheduled_commands:
            await self._scheduler._scheduler.start()
        await self.control.send_message(
            MessageContext(admin_room=False), f"TeslaBot {__version__} started"
        )
        if not self.authorized and self.control.run_scheduled_commands:
            generation = self.auth_generation
            try:
                authorization_url = await self._authorization_url(generation)
                if authorization_url is not None and generation == self.auth_generation and not self.authorized:
                    await self.control.send_message(
                        MessageContext(admin_room=True),
                        f'Not authorized. Authorization URL: {authorization_url} "Page Not Found" will be shown at success. Use !authorize https://the/url/you/ended/up/at',
                    )
            except (AppException, MessageSendError, asyncio.TimeoutError):
                logger.warning("Startup authorization notice dropped", exc_info=True)
        if self.control.run_scheduled_commands:
            assert self._scheduler._scheduler._task is not None
            await self._scheduler._scheduler._task
            raise AppException("Scheduler returned unexpectedly")
        await asyncio.Event().wait()

    async def close(self) -> None:
        if self._scheduler._scheduler._task is not None:
            await self._scheduler._scheduler.stop()
        async with self._operation(allow_transition=True):
            self.tesla.close()
