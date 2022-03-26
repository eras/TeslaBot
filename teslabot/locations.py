from configparser import ConfigParser
from typing import Dict, Any, Optional, Tuple, Callable, Awaitable, List, Union
from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from math import sin, cos, sqrt, atan2, radians
import logging

from .state import State, StateElement
from .control import CommandContext
from .utils import map_optional
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
class LatLon:
    lat: float
    lon: float

@dataclass
class Location:
    lat: float
    lon: float
    near_km: Optional[float] = None  # range for a point to be considered to be "near"
    address: Optional[str] = None

    def json(self) -> Any:
        js: Dict[str, Union[str, float]] = {"lat": self.lat, "lon": self.lon}
        if self.near_km is not None:
            js["near_km"] = self.near_km
        if self.address is not None:
            js["address"] = self.address
        return js

    def __str__(self) -> str:
        return f"Lat={self.lat} Lon={self.lon}"

    @staticmethod
    def from_json(json: Any) -> "Location":
        return Location(lat=json["lat"], lon=json["lon"],
                        near_km=json.get("near_km", None),
                        address=json.get("address", None))

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

class LocationInfo(ABC):
    @staticmethod
    def from_coords(coords: Tuple[Optional[str], ...]) -> "LocationInfo":
        assert coords[0] is not None
        assert coords[1] is not None
        return LocationInfoCoords(LatLon(float(coords[0]), float(coords[1])),
                                  map_optional(coords[2], float))

    @staticmethod
    def from_current(args: Tuple[Tuple[Optional[str], ...], Optional[str]]) -> "LocationInfo":
        current, vehicle_name = args
        near_km = map_optional(current[0], lambda x: float(x))
        return LocationInfoCurrent(vehicle_name=vehicle_name,
                                   near_km=near_km)

@dataclass
class LocationInfoCoords(LocationInfo):
    latlon: LatLon
    near_km: Optional[float]

@dataclass
class LocationInfoCurrent(LocationInfo):
    vehicle_name: Optional[str]
    near_km: Optional[float]
    def to_info_coords(self, latlon: LatLon) -> LocationInfoCoords:
        return LocationInfoCoords(latlon=latlon, near_km=self.near_km)

LocationRegexValue = \
    p.Regex(r"^([0-9]+(?:\.[0-9]+)?),([0-9]+(?:\.[0-9]+)?)(?:,([0-9]+(?:\.[0-9]+)?))?$")

LocationCoordsValue : p.Parser[LocationInfo] = \
    p.OneOf(p.Map(map=LocationInfo.from_coords, parser=LocationRegexValue),
            p.Map(map=LocationInfo.from_current,
                  parser=p.Adjacent(p.Regex(r"^current(?:,([0-9]+(\.[0-9]+)?))?$"),
                                    p.Optional_(p.AnyStr()))))

LocationAddArgs = Tuple[Tuple[str, LocationInfo], Optional[str]]
LocationAddArgsValue: p.Parser[LocationAddArgs] = \
    p.Remaining(p.Adjacent(p.Adjacent(p.AnyStr(),
                                      LocationCoordsValue),
                           p.ValidOrMissing(p.RestAsStr())))

LocationLsArgsValue = p.Empty()
LocationLsArgs = Tuple[()]

LocationRmArgsType = List[str]
LocationRmArgsValue: p.Parser[LocationRmArgsType] = p.Remaining(p.List_(p.AnyStr()))

class LocationCommandContextBase(ABC):
    cmd: CommandContext

    def __init__(self, context: CommandContext) -> None:
        self.cmd = context

    @abstractmethod
    async def get_location(self, vehicle_name: Optional[str]) -> Optional[LatLon]:
        ...

LocationArgs = Callable[[LocationCommandContextBase], Awaitable[None]]

def LocationArgsParser(locations: "Locations") -> p.Parser[LocationArgs]:
    return locations.cmds.parser()

class Locations(StateElement):
    locations: Dict[Name, Location]

    canonical_to_orig: Dict[str, str]
    """Maps from canonical names to real names. Used to detect duplicates also."""

    state: State
    cmds: commands.Commands[LocationCommandContextBase]

    def __init__(self, state: State) -> None:
        self.locations = {}
        self.canonical_to_orig = {}
        self.state = state
        state.add_element(self)

        self.cmds = commands.Commands[LocationCommandContextBase]()
        self.cmds.register(commands.Function("add", "Add a new location: add name lat,lon[,near_km] [address]  or  add name current,[near_km] [vehicle name] [address]",
                                             LocationAddArgsValue,
                                             self._command_location_add))
        self.cmds.register(commands.Function("ls", "List locations",
                                             LocationLsArgsValue,
                                             self._command_location_ls))
        self.cmds.register(commands.Function("rm", "Remove locations by name",
                                             LocationRmArgsValue,
                                             self._command_location_rm))

        self.load()

    def help(self) -> str:
        return self.cmds.help()

    async def command(self, context: LocationCommandContextBase, args: LocationArgs) -> None:
        await args(context)

    async def _command_location_add(self,
                                    context: LocationCommandContextBase,
                                    args: LocationAddArgs) -> None:
        (name, coords), address = args
        if isinstance(coords, LocationInfoCoords):
            loc_info: Optional[LocationInfoCoords] = coords
        else:
            assert isinstance(coords, LocationInfoCurrent)
            latlon = await context.get_location(coords.vehicle_name) # TODO
            if latlon:
                loc_info = coords.to_info_coords(latlon)
            else:
                loc_info = None
        if loc_info is None:
            await context.cmd.control.send_message(context.cmd.to_message_context(),
                                                   f"Cannot get current location")
        else:
            try:
                await self.add(name, Location(lat=loc_info.latlon.lat, lon=loc_info.latlon.lon,
                                              near_km=loc_info.near_km,
                                              address=address))
                await context.cmd.control.send_message(context.cmd.to_message_context(),
                                                       f"Added location {name}")
            except DuplicateLocationError as exn:
                await context.cmd.control.send_message(context.cmd.to_message_context(),
                                                       str(exn))

    async def _command_location_ls(self,
                                   context: LocationCommandContextBase,
                                   args: LocationLsArgs) -> None:
        if self.locations:
            def format_loc(name: str, loc: Location) -> str:
                st = f"{name}: lat/lon={loc.lat},{loc.lon}"
                if loc.near_km is not None:
                    st += f" near={loc.near_km} km"
                if loc.address is not None:
                    st += f" address={loc.address}"
                return st
            locs_str = "\n".join([format_loc(name, loc) for name, loc in self.locations.items()])
            await context.cmd.control.send_message(context.cmd.to_message_context(),
                                               f"Locations:\n{locs_str}")
        else:
            await context.cmd.control.send_message(context.cmd.to_message_context(),
                                               f"No locations")

    async def _command_location_rm(self,
                                   context: LocationCommandContextBase,
                                   args: LocationRmArgsType) -> None:
        names = args
        try:
            for name in names:
                await self.remove(name)
                await context.cmd.control.send_message(context.cmd.to_message_context(),
                                                       f"Removed location {name}")
        except NoSuchLocationError as exn:
            await context.cmd.control.send_message(context.cmd.to_message_context(),
                                               str(exn))


    # StateElement.save
    async def save(self, state: ConfigParser) -> None:
        if not "locations" in state:
            state["locations"] = {}
        locations = state["locations"]
        locations.clear()
        for name, location in self.locations.items():
            locations[name] = json.dumps({"name": name, "location": location.json()})

    def load(self) -> None:
        if not self.state.state.has_section("locations"):
            return
        for name, location in self.state.state["locations"].items():
            if "location" in location:
                data = json.loads(location)
                orig_name = data["name"]
                location_data = data["location"]
            else:
                orig_name = name
                location_data = json.loads(location)
            self.locations[orig_name] = Location.from_json(location_data)
            self.canonical_to_orig[self._canonical(orig_name)] = orig_name

    def _canonical(self, name: str) -> str:
        return name.lower()

    async def add(self, name: str, location: Location) -> None:
        canonical = self._canonical(name)
        if canonical in self.canonical_to_orig:
            raise DuplicateLocationError(f"Already has location {self.canonical_to_orig[canonical]}")
        self.locations[name] = location
        self.canonical_to_orig[canonical] = name
        await self.state.save()

    async def remove(self, name: str) -> None:
        canonical = self._canonical(name)
        if canonical not in self.canonical_to_orig:
            raise NoSuchLocationError(f"No location {name} to remove")
        del self.locations[self.canonical_to_orig[canonical]]
        del self.canonical_to_orig[canonical]
        await self.state.save()

    def nearest_location(self, location: Location) -> Tuple[Optional[str], Optional[Location]]:
        # The return type is just more practical on Python this way.. At least before Python 3.9.
        nearest: Optional[Tuple[str, Location, float]] = None
        for name, loc_candidate in self.locations.items():
            if not nearest or loc_candidate.km_to(location) < nearest[2]:
                nearest = (name, loc_candidate, loc_candidate.km_to(location))

        return (nearest[0], nearest[1]) if nearest else (None, None)
