import asyncio
import re

from dataclasses import dataclass
from nio import AsyncClient, MatrixRoom, RoomMessageText
from nio.responses import LoginResponse
from configparser import ConfigParser
from typing import Optional

from . import control
from .config import Config, ConfigElement

class ConfigSave(ConfigElement):
    control: "MatrixControl"

    def __init__(self, control: "MatrixControl") -> None:
        self.control = control

    def save(self, config: ConfigParser) -> None:
        if self.control.login_response:
            config["matrix"]["mxid"]         = self.control.login_response.user_id
            config["matrix"]["device_id"]    = self.control.login_response.device_id
            config["matrix"]["access_token"] = self.control.login_response.access_token

class MatrixControl(control.Control):
    client: AsyncClient
    room_id: str
    config: Config
    login_response: Optional[LoginResponse]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.config.add_element(ConfigSave(self))
        self.client = AsyncClient(self.config.config["matrix"]["homeserver"],
                                  self.config.config["matrix"]["mxid"])
        self.client.add_event_callback(self._message_callback, RoomMessageText)
        self.login_response = None

    async def setup(self) -> None:
        mx_config = self.config.config["matrix"]
        if "matrix" in self.config.config and "access_token" in self.config.config["matrix"]:
            logger.debug(f"Using pre-existing credentials")
            self.client.user_id      = mx_config["mxid"]
            self.client.device_id    = mx_config["device_id"]
            self.client.access_token = mx_config["access_token"]
        else:
            logger.debug(f"Logging in")
            login = await self.client.login(self.config.config["matrix"]["password"])

            # "homeserver": homeserver,  # e.g. "https://matrix.example.org"
            # "mxid": resp.user_id,  # e.g. "@user:example.org"
            # "device_id": resp.device_id,  # device ID, 10 uppercase letters
            # "access_token": resp.access_token  # cryptogr. access token

    async def send_message(self, message: str) -> None:
        await self.client.room_send(
            room_id=self.room_id,
            message_type="m.room.message",
            content = {
                "msgtype": "m.text",
                "body": message
            }
        )

    async def _message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if room.room_id == self.room_id and re.match(r"^!", event.body):
            await self.callback.command_callback(event.body[1:])
        # print(
        #     f"Message received in room {room.display_name}\n"
        #     f"{room.user_name(event.sender)} | {event.body}"
        # )

    async def run(self) -> None:
        await self.client.sync_forever(timeout=30000)
