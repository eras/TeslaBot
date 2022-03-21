from configparser import ConfigParser
from typing import Dict, Any, Optional, Tuple, Callable, Awaitable, List, Union
from dataclasses import dataclass
import json
from math import sin, cos, sqrt, atan2, radians
import logging

from .state import State, StateElement
from .control import CommandContext
from . import commands
from . import parser as p
from . import log

logger = log.getLogger(__name__)

Name = str

class LocationsException(Exception):
    pass

class DuplicateLocationError(LocationsException):
    pass

class NoSuchLocationError(LocationsException):
    pass

@dataclass
class Location:
    lat: float
    lon: float
    address: Optional[str] = None

    def json(self) -> Any:
        js: Dict[str, Union[str, float]] = {"lat": self.lat, "lon": self.lon}
        if self.address is not None:
            js["address"] = self.address
        return js

    def __str__(self) -> str:
        return f"Lat={self.lat} Lon={self.lon}"

    @staticmethod
    def from_json(json: Any) -> "Location":
        return Location(lat=json["lat"], lon=json["lon"], address=json.get("address", None))

    def km_to(self, other: "Location") -> float:
        point1 = self
        point2 = other

        # https://stackoverflow.com/a/57294783
        R = 6370
        lat1 = radians(point1.lat)
        lon1 = radians(point1.lon)
        lat2 = radians(point2.lat)
        lon2 = radians(point2.lon)

        dlon = lon2 - lon1
        dlat = lat2 - lat1

        a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
        c = 2 * atan2(sqrt(a), sqrt(1-a))
        distance = R * c
        return distance

    def url(self) -> str:
        return f"https://www.openstreetmap.org/?mlat={self.lat}&mlon={self.lon}"

LocationAddArgsValue = \
    p.Remaining(p.Adjacent(p.Adjacent(p.AnyStr(),
                                     p.Regex(r"^([0-9]+(\.[0-9]+)?),([0-9]+(\.[0-9]+)?)$", [1, 3])),
                          p.ValidOrMissing(p.RestAsStr())))
LocationAddArgs = Tuple[Tuple[str, Tuple[str, ...]], Optional[str]]

LocationLsArgsValue = p.Empty()
LocationLsArgs = Tuple[()]

LocationRmArgsValue = p.Remaining(p.AnyStr())
LocationRmArgs = str

LocationArgs = Callable[[CommandContext], Awaitable[None]]

def LocationArgsParser(locations: "Locations") -> p.Parser[LocationArgs]:
    return locations.cmds.parser()

class Locations(StateElement):
    locations: Dict[Name, Location]

    canonical_names: Dict[str, str]
    """Maps from canonical names to real names. Used to detect duplicates also."""

    state: State
    cmds: commands.Commands[CommandContext]

    def __init__(self, state: State) -> None:
        self.locations = {}
        self.canonical_names = {}
        self.state = state
        state.add_element(self)

        self.cmds = commands.Commands[CommandContext]()
        self.cmds.register(commands.Function("add", "Add a new location: add name lat,lon [address]",
                                             LocationAddArgsValue,
                                             self._command_location_add))
        self.cmds.register(commands.Function("ls", "List locations",
                                             LocationLsArgsValue,
                                             self._command_location_ls))
        self.cmds.register(commands.Function("rm", "Remove location by name",
                                             LocationRmArgsValue,
                                             self._command_location_rm))

        self.load()

    def help(self) -> str:
        return self.cmds.help()

    async def command(self, context: CommandContext, args: LocationArgs) -> None:
        await args(context)

    async def _command_location_add(self,
                                    context: CommandContext,
                                    args: LocationAddArgs) -> None:
        (name, (lat, lon)), address = args
        try:
            await self.add(name, Location(lat=float(lat), lon=float(lon), address=address))
            await context.control.send_message(context.to_message_context(),
                                               f"Added location {name}")
        except DuplicateLocationError as exn:
            await context.control.send_message(context.to_message_context(),
                                               str(exn))

    async def _command_location_ls(self,
                                   context: CommandContext,
                                   args: LocationLsArgs) -> None:
        if self.locations:
            def format_loc(name: str, loc: Location) -> str:
                st = f"{name}: lat={loc.lat} lon={loc.lon}"
                if loc.address is not None:
                    st += f" address={loc.address}"
                return st
            locs_str = "\n".join([format_loc(name, loc) for name, loc in self.locations.items()])
            await context.control.send_message(context.to_message_context(),
                                               f"Locations:\n{locs_str}")
        else:
            await context.control.send_message(context.to_message_context(),
                                               f"No locations")

    async def _command_location_rm(self,
                                   context: CommandContext,
                                   args: LocationRmArgs) -> None:
        name = args
        try:
            await self.remove(name)
            await context.control.send_message(context.to_message_context(),
                                               f"Removed location {name}")
        except NoSuchLocationError as exn:
            await context.control.send_message(context.to_message_context(),
                                               str(exn))


    # StateElement.save
    async def save(self, state: ConfigParser) -> None:
        if not "locations" in state:
            state["locations"] = {}
        locations = state["locations"]
        for name, location in self.locations.items():
            locations[name] = json.dumps(location.json())

    def load(self) -> None:
        if not self.state.state.has_section("locations"):
            return
        for name, location in self.state.state["locations"].items():
            self.locations[name] = Location.from_json(json.loads(location))
            self.canonical_names[self._canonical(name)] = name

    def _canonical(self, name: str) -> str:
        return name.lower()

    async def add(self, name: str, location: Location) -> None:
        canonical = self._canonical(name)
        if canonical in self.canonical_names:
            raise DuplicateLocationError(f"Already has location {self.canonical_names[canonical]}")
        self.locations[name] = location
        self.canonical_names[canonical] = name
        await self.state.save()

    async def remove(self, name: str) -> None:
        canonical = self._canonical(name)
        if canonical not in self.canonical_names:
            raise NoSuchLocationError(f"No location {name} to remove")
        del self.locations[self.canonical_names[canonical]]
        del self.canonical_names[canonical]
        await self.state.save()

    def nearest_location(self, location: Location) -> Tuple[Optional[str], Optional[Location]]:
        # The return type is just more practical on Python this way.. At least before Python 3.9.
        nearest: Optional[Tuple[str, Location, float]] = None
        for name, loc_candidate in self.locations.items():
            if not nearest or loc_candidate.km_to(location) < nearest[2]:
                nearest = (name, loc_candidate, loc_candidate.km_to(location))

        return (nearest[0], nearest[1]) if nearest else (None, None)
