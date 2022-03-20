import asyncio
import re
import os
import errno
from nio import Event, AsyncClient, MatrixRoom, RoomMessageText, InviteEvent
from nio.responses import LoginError, LoginResponse, SyncResponse
from nio.exceptions import OlmUnverifiedDeviceError
from configparser import ConfigParser
from typing import Optional, List, Callable, Coroutine, Any, Tuple

from . import control
from .control import CommandContext
from .utils import get_optional
from .config import Config
from .state import State, StateElement
from . import log
from .env import Env
from . import commands, parser

logger = log.getLogger(__name__)

class StateSave(StateElement):
    control: "MatrixControl"

    def __init__(self, control: "MatrixControl") -> None:
        self.control = control

    async def save(self, state: ConfigParser) -> None:
        if self.control._logged_in:
            if not "matrix" in state:
                state["matrix"] = {}
            st = state["matrix"]
            st["admin_room_id"]= get_optional(self.control._admin_room_id, "")
            st["room_id"]      = get_optional(self.control._room_id, "")
            st["sync_token"]   = get_optional(self.control._sync_token, "")
            st["device_id"]    = get_optional(self.control._client.device_id, "")
            st["access_token"] = self.control._client.access_token

class MatrixControl(control.Control):
    _client: AsyncClient
    _admin_room_id: Optional[str]
    _room_id: Optional[str]
    _config: Config
    _state: State
    _logged_in: bool
    _sync_token: Optional[str]
    _init_done: asyncio.Event

    _pending_event_handlers: List[Callable[[], Coroutine[Any, Any, None]]]
    """Handlers created for received messages during initial sync that we cannot quite handle yet are pushed here."""

    def __init__(self, env: Env) -> None:
        super().__init__()
        self._config = env.config
        self._state = env.state
        self._init_done = asyncio.Event()

        store_path = self._config.get("matrix", "store_path")
        try:
            os.makedirs(store_path)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        self._pending_event_handlers = []

        self._state.add_element(StateSave(self))
        self._client = AsyncClient(self._config.get("matrix", "homeserver"),
                                   self._config.get("matrix", "mxid"),
                                   store_path=store_path)
        self._logged_in = False
        self._sync_token = None
        if "sync_token" in self._state.state["matrix"] is not None and \
           self._state.state["matrix"]["sync_token"] != "":
            self._sync_token = self._state.state["matrix"]["sync_token"]
        room_id = self._state.state["matrix"]["room_id"] if "room_id" in self._state.state["matrix"] else None
        if room_id == "":
            room_id = None
        self._room_id = room_id
        admin_room_id = self._state.state["matrix"]["admin_room_id"] if "admin_room_id" in self._state.state["matrix"] else None
        if admin_room_id == "":
            admin_room_id = None
        self._admin_room_id = admin_room_id

        self.local_commands.register(commands.Function("sameroom", "Assign control room to be the same as admin room",
                                                       parser.Empty(), self._command_sameroom))

    async def _command_sameroom(self, context: CommandContext, args: Tuple[()]) -> None:
        if context.admin_room:
            await self.send_message(context.to_message_context(), f"Setting room_id = self._admin_room_id (was {self._room_id})")
            self._room_id = self._admin_room_id
            await self._state.save()
        else:
            await self.send_message(context.to_message_context(), "This request must be sent to the admin room.")

    async def setup(self) -> None:
        mx_config = self._config["matrix"] if self._config.has_section("matrix") else None
        mx_state = self._state.state["matrix"] if "matrix" in self._state.state else None
        if mx_config is None:
            logger.error(f"Cannot setup matrix due to missing configuration")
            return
        if mx_state is not None and "access_token" in mx_state and mx_state["access_token"] != "":
            self._logged_in = True
            logger.debug(f"Using pre-existing credentials")
            self._client.restore_login(user_id=mx_config["mxid"],
                                       device_id=mx_state["device_id"],
                                       access_token=mx_state["access_token"])
        else:
            logger.debug(f"Logging in")
            login = await self._client.login(mx_config["password"])
            if isinstance(login, LoginError):
                logger.error(f"Failed to log in")
            elif isinstance(login, LoginResponse):
                self._logged_in = True
                logger.info(f"Login successful")
                await self._state.save()

    async def send_message(self,
                           message_context: control.MessageContext,
                           message: str) -> None:
        room_id = self._admin_room_id if message_context.admin_room else self._room_id
        if room_id is None:
            logger.error(f"No room id known, cannot send \"{message}\"")
        else:
            logger.debug(f"send_message wait ready start")
            await self.wait_ready()
            logger.debug(f"send_message wait ready done")
            logger.info(f"> {message}")
            try:
                await self._client.room_send(
                    room_id=room_id,
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
        if self._admin_room_id is None:
            logger.debug(f"invite callback to {room} event {event}: joining to admin room")
            await self._client.join(room.room_id)
            self._admin_room_id = room.room_id
            await self._state.save()
            logger.info(f"Room {room.name} is encrypted: {room.encrypted}")
            await self.send_message(control.MessageContext(admin_room=True), "This is the admin room. Invite to another room or use !sameroom to set this to be the control room as well.")
        elif self._room_id is None:
            logger.debug(f"invite callback to {room} event {event}: joining to control room")
            await self._client.join(room.room_id)
            self._room_id = room.room_id
            await self._state.save()
            logger.info(f"Room {room.name} is encrypted: {room.encrypted}")
            await self.send_message(control.MessageContext(admin_room=False), "This is the control room.")
        else:
            logger.debug(f"invite callback to {room} event {event}: not joining, we are already in {self._room_id}")

    async def _message_callback(self, room: MatrixRoom, event: Event) -> None:
        if self._init_done.is_set():
            assert isinstance(event, RoomMessageText)
            if [self._admin_room_id, self._room_id].count(room.room_id):
                admin_room = room.room_id == self._admin_room_id
                command_context = CommandContext(admin_room=admin_room, control=self)
                await self.process_message(command_context, event.body)
        else:
            self._pending_event_handlers.append(lambda: self._message_callback(room, event))

    async def _sync_callback(self, response: SyncResponse) -> None:
        self._sync_token = response.next_batch
        await self._state.save()

    def trust_devices(self, user_id: str, device_list: Optional[str] = None) -> None:
        # https://matrix-nio.readthedocs.io/en/latest/examples.html?highlight=invite#manual-encryption-key-verification
        logger.info(f"Trusting {user_id} {device_list}")
        for device_id, olm_device in self._client.device_store[user_id].items():
            if device_list and device_id not in device_list:
                # a list of trusted devices was provided, but this ID is not in
                # that list. That's an issue.
                logger.info(f"Not trusting {device_id} as it's not in {user_id}'s pre-approved list.")
                continue

            if user_id == self._client.user_id and device_id == self._client.device_id:
                continue

            self._client.verify_device(olm_device)
            logger.info(f"Trusting {device_id} from user {user_id}")

    async def wait_ready(self) -> None:
        await self._init_done.wait()

    async def run(self) -> None:
        if not self._logged_in:
            logger.error(f"Cannot run, not logged in")
            return
        self._client.add_response_callback(self._sync_callback, SyncResponse) # type: ignore
        self._client.add_event_callback(self._message_callback, RoomMessageText)
        self._client.add_event_callback(self._invite_callback, InviteEvent) # type: ignore
        async def after_first_sync() -> None:
            logger.debug(f"after_first_sync synced wait")
            await self._client.synced.wait()
            logger.debug(f"after_first_sync synced wait done")
            for mxid in self._config["matrix"]["trust_mxids"].split(","):
                # TODO: implement proper verification, trusting just mxids in particular is not safe
                self.trust_devices(mxid)
            self._init_done.set()
            for pending in self._pending_event_handlers:
                await pending()
            self._pending_event_handlers = []
        # https://matrix-nio.readthedocs.io/en/latest/examples.html?highlight=invite#manual-encryption-key-verification
        after_first_sync_task = asyncio.ensure_future(after_first_sync())
        sync_forever_task = asyncio.ensure_future(self._client.sync_forever(timeout=30000, since=self._sync_token, full_state=True))
        logger.info(f"Sync starts")
        await asyncio.gather(
            # The order here IS significant! You have to register the task to trust
            # devices FIRST since it awaits the first sync
            after_first_sync_task,
            sync_forever_task
        )
