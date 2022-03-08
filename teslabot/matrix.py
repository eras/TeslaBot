import asyncio
import re
import os
import errno
from nio import Event, AsyncClient, MatrixRoom, RoomMessageText, InviteEvent
from nio.responses import LoginError, LoginResponse, SyncResponse
from nio.exceptions import OlmUnverifiedDeviceError
from configparser import ConfigParser
from typing import Optional, List

from . import control
from .utils import get_optional
from .config import Config
from .state import State, StateElement
from . import log
from .env import Env
from . import commands

logger = log.getLogger(__name__)
logger.setLevel(log.DEBUG)

class StateSave(StateElement):
    control: "MatrixControl"

    def __init__(self, control: "MatrixControl") -> None:
        self.control = control

    def save(self, state: ConfigParser) -> None:
        if self.control.logged_in:
            if not "matrix" in state:
                state["matrix"] = {}
            st = state["matrix"]
            st["room_id"]      = get_optional(self.control.room_id, "")
            st["sync_token"]   = get_optional(self.control.sync_token, "")
            st["device_id"]    = get_optional(self.control.client.device_id, "")
            st["access_token"] = self.control.client.access_token

class MatrixControl(control.Control):
    client: AsyncClient
    room_id: Optional[str]
    config: Config
    state: State
    logged_in: bool
    sync_token: Optional[str]
    local_commands: commands.Commands[None]

    def __init__(self, env: Env) -> None:
        super().__init__()
        self.config = env.config
        self.state = env.state

        store_path = self.config.config["matrix"]["store_path"]
        try:
            os.makedirs(store_path)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        self.local_commands = commands.Commands()
        self.local_commands.register(commands.Function[None]("ping", self._command_ping))

        self.state.add_element(StateSave(self))
        self.client = AsyncClient(self.config.config["matrix"]["homeserver"],
                                  self.config.config["matrix"]["mxid"],
                                  store_path=store_path)
        self.logged_in = False
        self.sync_token = None
        if "sync_token" in self.state.state["matrix"] is not None and \
           self.state.state["matrix"]["sync_token"] != "":
            self.sync_token = self.state.state["matrix"]["sync_token"]
        room_id = self.state.state["matrix"]["room_id"] if "room_id" in self.state.state["matrix"] else None
        if room_id == "":
            room_id = None
        self.room_id = room_id

    async def setup(self) -> None:
        mx_config = self.config.config["matrix"] if "matrix" in "matrix" in self.config.config else None
        mx_state = self.state.state["matrix"] if "matrix" in "matrix" in self.state.state else None
        if mx_config is None:
            logger.error(f"Cannot setup matrix due to missing configuration")
            return
        if mx_state is not None and "access_token" in mx_state and mx_state["access_token"] != "":
            self.logged_in = True
            logger.debug(f"Using pre-existing credentials")
            self.client.restore_login(user_id=mx_config["mxid"],
                                      device_id=mx_state["device_id"],
                                      access_token=mx_state["access_token"])
        else:
            logger.debug(f"Logging in")
            login = await self.client.login(mx_config["password"])
            if isinstance(login, LoginError):
                logger.error(f"Failed to log in")
            elif isinstance(login, LoginResponse):
                self.logged_in = True
                logger.info(f"Login successful")
                self.state.save()

    async def _command_ping(self, context: None, args: List[str]) -> None:
        await self.send_message("pong")

    async def send_message(self, message: str) -> None:
        if self.room_id is None:
            logger.error(f"No room id known, cannot send \"{message}\"")
        else:
            logger.info(f"> {message}")
            try:
                await self.client.room_send(
                    room_id=self.room_id,
                    message_type="m.room.message",
                    content = {
                        "msgtype": "m.notice", # or m.text
                        "body": message
                    })
            except OlmUnverifiedDeviceError as err:
                logger.error(f"Cannot send message due to verification error: {err}")
                # logger.info(f"These are all known devices:")
                # device_store: crypto.DeviceStore = device_store
                # [logger.info(f"\t{device.user_id}\t {device.device_id}\t {device.trust_state}\t  {device.display_name}") for device in device_store]
                pass

    async def _invite_callback(self, room: MatrixRoom, event: Event) -> None:
        assert isinstance(event, InviteEvent)
        if self.room_id is None:
            logger.debug(f"invite callback to {room} event {event}: joining")
            await self.client.join(room.room_id)
            self.room_id = room.room_id
            self.state.save()
            print(f"Room {room.name} is encrypted: {room.encrypted}" )
        else:
            logger.debug(f"invite callback to {room} event {event}: not joining, we are already in {self.room_id}")

    async def _message_callback(self, room: MatrixRoom, event: Event) -> None:
        assert isinstance(event, RoomMessageText)
        if room.room_id == self.room_id and re.match(r"^!", event.body):
            try:
                invocation = commands.Invocation.parse(event.body[1:])
                if self.local_commands.has_command(invocation.name):
                    await self.local_commands.invoke(None, invocation)
                else:
                    await self.callback.command_callback(invocation)
            except commands.InvocationParseError:
                logger.error(f"Failed to parse command: {event.body[1:]}")
        # print(
        #     f"Message received in room {room.display_name}\n"
        #     f"{room.user_name(event.sender)} | {event.body}"
        # )

    async def _sync_callback(self, response: SyncResponse) -> None:
        logger.debug(f"sync callback: {type(response)} {response}")
        self.sync_token = response.next_batch
        self.state.save()

    def trust_devices(self, user_id: str, device_list: Optional[str] = None) -> None:
        # https://matrix-nio.readthedocs.io/en/latest/examples.html?highlight=invite#manual-encryption-key-verification
        logger.info(f"Trusting {user_id} {device_list}")
        for device_id, olm_device in self.client.device_store[user_id].items():
            if device_list and device_id not in device_list:
                # a list of trusted devices was provided, but this ID is not in
                # that list. That's an issue.
                logger.info(f"Not trusting {device_id} as it's not in {user_id}'s pre-approved list.")
                continue

            if user_id == self.client.user_id and device_id == self.client.device_id:
                continue

            self.client.verify_device(olm_device)
            logger.info(f"Trusting {device_id} from user {user_id}")

    async def run(self) -> None:
        if not self.logged_in:
            logger.error(f"Cannot run, not logged in")
            return
        self.client.add_response_callback(self._sync_callback, SyncResponse) # type: ignore
        self.client.add_event_callback(self._message_callback, RoomMessageText)
        self.client.add_event_callback(self._invite_callback, InviteEvent) # type: ignore
        async def after_first_sync():
            await self.client.synced.wait()
            for mxid in self.config.config["matrix"]["trust_mxids"].split(","):
                # TODO: implement proper verification, trusting just mxids in particular is not safe
                self.trust_devices(mxid)
        # https://matrix-nio.readthedocs.io/en/latest/examples.html?highlight=invite#manual-encryption-key-verification
        after_first_sync_task = asyncio.ensure_future(after_first_sync())
        sync_forever_task = asyncio.ensure_future(self.client.sync_forever(timeout=30000, since=self.sync_token, full_state=True))
        logger.info(f"Sync starts")
        await asyncio.gather(
            # The order here IS significant! You have to register the task to trust
            # devices FIRST since it awaits the first sync
            after_first_sync_task,
            sync_forever_task
        )
