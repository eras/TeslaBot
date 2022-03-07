import re

from nio import Event, AsyncClient, MatrixRoom, RoomMessageText, InviteEvent
from nio.responses import LoginError, LoginResponse, SyncResponse
from configparser import ConfigParser
from typing import Optional

from . import control
from .utils import get_optional
from .config import Config, ConfigElement
from . import log

logger = log.getLogger(__name__)
logger.setLevel(log.DEBUG)

class ConfigSave(ConfigElement):
    control: "MatrixControl"

    def __init__(self, control: "MatrixControl") -> None:
        self.control = control

    def save(self, config: ConfigParser) -> None:
        if self.control.logged_in:
            config["matrix"]["room_id"]      = get_optional(self.control.room_id, "")
            config["matrix"]["sync_token"]   = get_optional(self.control.sync_token, "")
            config["matrix"]["mxid"]         = self.control.client.user_id
            config["matrix"]["device_id"]    = get_optional(self.control.client.device_id, "")
            config["matrix"]["access_token"] = self.control.client.access_token

class MatrixControl(control.Control):
    client: AsyncClient
    room_id: Optional[str]
    config: Config
    logged_in: bool
    sync_token: Optional[str]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.config.add_element(ConfigSave(self))
        self.client = AsyncClient(self.config.config["matrix"]["homeserver"],
                                  self.config.config["matrix"]["mxid"])
        self.logged_in = False
        self.sync_token = None
        if "sync_token" in self.config.config["matrix"] is not None and \
           self.config.config["matrix"]["sync_token"] != "":
            self.sync_token = self.config.config["matrix"]["sync_token"]
        room_id = self.config.config["matrix"]["roomid"] if "roomid" in self.config.config["matrix"] else None
        if room_id == "":
            room_id = None
        self.room_id = room_id

    async def setup(self) -> None:
        mx_config = self.config.config["matrix"]
        if "matrix" in self.config.config and \
           "access_token" in self.config.config["matrix"] and \
           self.config.config["matrix"]["access_token"] != "":
            self.logged_in = True
            logger.debug(f"Using pre-existing credentials")
            self.client.user_id      = mx_config["mxid"]
            self.client.device_id    = mx_config["device_id"]
            self.client.access_token = mx_config["access_token"]
        else:
            logger.debug(f"Logging in")
            login = await self.client.login(self.config.config["matrix"]["password"])
            if isinstance(login, LoginError):
                logger.error(f"Failed to log in")
            elif isinstance(login, LoginResponse):
                self.logged_in = True
                logger.info(f"Login successful")
                self.config.save()

    async def send_message(self, message: str) -> None:
        if self.room_id is None:
            logger.error(f"No room id known, cannot send \"{message}\"")
        else:
            logger.info(f"> {message}")
            await self.client.room_send(
                room_id=self.room_id,
                message_type="m.room.message",
                content = {
                    "msgtype": "m.text",
                    "body": message
                }
        )

    async def _invite_callback(self, room: MatrixRoom, event: Event) -> None:
        assert isinstance(event, InviteEvent)
        if self.room_id is None:
            logger.debug(f"invite callback to {room} event {event}: joining")
            await self.client.join(room.room_id)
            self.room_id = room.room_id
            self.config.save()
            print(f"Room {room.name} is encrypted: {room.encrypted}" )
        else:
            logger.debug(f"invite callback to {room} event {event}: not joining, we are already in {self.room_id}")

    async def _message_callback(self, room: MatrixRoom, event: Event) -> None:
        assert isinstance(event, RoomMessageText)
        if room.room_id == self.room_id and re.match(r"^!", event.body):
            await self.callback.command_callback(event.body[1:])
        # print(
        #     f"Message received in room {room.display_name}\n"
        #     f"{room.user_name(event.sender)} | {event.body}"
        # )

    async def _sync_callback(self, response: SyncResponse) -> None:
        logger.debug(f"sync callback: {type(response)} {response}")
        self.sync_token = response.next_batch
        self.config.save()

    async def run(self) -> None:
        self.client.add_response_callback(self._sync_callback, SyncResponse) # type: ignore
        self.client.add_event_callback(self._message_callback, RoomMessageText)
        self.client.add_event_callback(self._invite_callback, InviteEvent) # type: ignore
        await self.client.sync_forever(timeout=30000, since=self.sync_token)
