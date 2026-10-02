import asyncio
import math
import aiohttp
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

    async def save(self, state: State) -> None:
        if self.control._logged_in:
            if not state.has_section("matrix"):
                state["matrix"] = {}
            st = state["matrix"]
            st["admin_room_id"]= get_optional(self.control._admin_room_id, "")
            st["room_id"]      = get_optional(self.control._room_id, "")
            st["sync_token"]   = get_optional(self.control._sync_token, "")
            st["device_id"]    = get_optional(self.control._client.device_id, "")
            st["access_token"] = self.control._client.access_token

class MatrixControl(control.Control):
    # Matrix bounds readiness and delivery independently, including local sends.
    message_timeout: Optional[float] = None
    readiness_timeout = 120.0
    send_timeout = 120.0
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
        self._send_tasks: dict[asyncio.Task[None], asyncio.Event] = {}
        self._delivery_lock = asyncio.Lock()
        self._close_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        for name in ("readiness_timeout", "send_timeout"):
            value = self._config.get("matrix", name, fallback="120")
            try:
                timeout = float(value)
            except ValueError as exn:
                raise control.ConfigError(f"matrix.{name} must be positive finite seconds, got {value!r}") from exn
            if not math.isfinite(timeout) or timeout <= 0:
                raise control.ConfigError(f"matrix.{name} must be positive finite seconds, got {value!r}")
            setattr(self, name, timeout)

        store_path = self._config.get("matrix", "store_path")
        try:
            os.makedirs(store_path)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        self._pending_event_handlers = []

        self._state.add_element(StateSave(self))
        if not self._state.has_section("matrix"):
            self._state["matrix"] = {}
        self._client = AsyncClient(self._config.get("matrix", "homeserver"),
                                   self._config.get("matrix", "mxid"),
                                   store_path=store_path)
        self._logged_in = False
        self._sync_token = None
        if self._state["matrix"].has_key("sync_token") and \
           self._state["matrix"]["sync_token"] != "":
            self._sync_token = self._state["matrix"]["sync_token"]
        room_id = self._state["matrix"]["room_id"] if self._state["matrix"].has_key("room_id") else None
        if room_id == "":
            room_id = None
        self._room_id = room_id
        admin_room_id = self._state["matrix"]["admin_room_id"] if self._state["matrix"].has_key("admin_room_id") else None
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
        mx_state = self._state["matrix"] if self._state.has_section("matrix") else None
        if mx_config is None:
            raise control.ConfigError("Matrix configuration missing")
        if mx_state is not None and mx_state.has_key("access_token") and mx_state["access_token"] != "":
            self._logged_in = True
            logger.debug(f"Using pre-existing credentials")
            self._client.restore_login(user_id=mx_config["mxid"],
                                       device_id=mx_state["device_id"],
                                       access_token=mx_state["access_token"])
        else:
            logger.debug(f"Logging in")
            login = await self._client.login(mx_config["password"])
            if isinstance(login, LoginError):
                logger.error("Matrix login failed: %s", login)
                raise control.ConfigError("Matrix login failed")
            elif isinstance(login, LoginResponse):
                self._logged_in = True
                logger.info(f"Login successful")
                await self._state.save()

    async def send_message(self,
                           message_context: control.MessageContext,
                           message: str) -> None:
        if self._closed:
            raise control.MessageSendError("Matrix control is closed")
        cancellation = asyncio.Event()
        task = asyncio.create_task(self._send_message(message_context, message, cancellation))
        self._send_tasks[task] = cancellation
        try:
            await self._join(task, cancellation)
        finally:
            self._send_tasks.pop(task, None)

    async def _join(self, task: asyncio.Task[None], cancellation: Optional[asyncio.Event] = None) -> None:
        cancelled = False
        while not task.done():
            try:
                # Waiting must not forward another cancellation into SDK cleanup.
                await asyncio.wait([task])
            except asyncio.CancelledError:
                cancelled = True
                if cancellation is not None:
                    cancellation.set()
        if cancelled:
            exn = None if task.cancelled() else task.exception()
            if exn is not None:
                logger.error("Matrix operation failed while draining cancellation", exc_info=(type(exn), exn, exn.__traceback__))
            raise asyncio.CancelledError
        task.result()

    async def _phase(self, work: Coroutine[Any, Any, Any], timeout: float, cancellation: asyncio.Event) -> Any:
        if cancellation.is_set():
            work.close()
            raise asyncio.CancelledError
        task = asyncio.create_task(work)
        interrupted = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait([task, interrupted], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if task in done and not cancellation.is_set():
                return task.result()
            # Cancel once, then join the real operation (including HTTP children)
            # without forwarding repeated caller/close cancellation into its drain.
            if not task.done():
                task.cancel()
            await asyncio.wait([task])
            try:
                task.result()
            except asyncio.CancelledError as exn:
                if cancellation.is_set():
                    raise
                raise asyncio.TimeoutError from exn
            if cancellation.is_set():
                raise asyncio.CancelledError
            raise asyncio.TimeoutError
        finally:
            interrupted.cancel()
            await asyncio.gather(interrupted, return_exceptions=True)

    async def _room_send(self, room_id: str, message: str) -> Any:
        async with self._delivery_lock:
            owner = asyncio.current_task()
            owned_event: Optional[asyncio.Event] = None
            original = self._client.get_missing_sessions
            target_room = room_id
            def capture(room_id: str) -> Any:
                nonlocal owned_event
                # nio creates its sharing event immediately before this call,
                # but its cleanup try/finally starts only after keys_claim.
                if room_id == target_room and asyncio.current_task() is owner:
                    owned_event = self._client.sharing_session.get(room_id)
                return original(room_id)
            self._client.get_missing_sessions = capture
            try:
                return await self._client.room_send(room_id=room_id, message_type="m.room.message",
                                                    content={"msgtype": "m.notice", "body": message})
            finally:
                self._client.get_missing_sessions = original
                if owned_event is not None and not owned_event.is_set():
                    if self._client.sharing_session.get(room_id) is owned_event:
                        self._client.sharing_session.pop(room_id)
                    logger.warning("Releasing owned interrupted Matrix key-claim sharing event for room %s; message %s",
                                   room_id, message, exc_info=True)
                    owned_event.set()

    async def _send_message(self,
                            message_context: control.MessageContext,
                            message: str, cancellation: asyncio.Event) -> None:
        room_id = self._admin_room_id if message_context.admin_room else self._room_id
        if room_id is None:
            logger.error("No room id known, cannot send %s", message)
            raise control.MessageSendError("Matrix destination unavailable")
        else:
            logger.debug(f"send_message wait ready start")
            try:
                await self._phase(self.wait_ready(), self.readiness_timeout, cancellation)
            except asyncio.TimeoutError as exn:
                logger.warning("Matrix readiness for room %s timed out after %s seconds; message %s",
                               room_id, self.readiness_timeout, message, exc_info=True)
                raise control.MessageSendError(f"Matrix readiness timed out after {self.readiness_timeout} seconds for {room_id}") from exn
            logger.debug(f"send_message wait ready done")
            logger.info("> %s", message)
            try:
                response = await self._phase(self._room_send(room_id, message), self.send_timeout, cancellation)
                if not hasattr(response, "event_id"):
                    raise control.MessageSendError(f"Matrix send failed: {response}")
            except OlmUnverifiedDeviceError as err:
                logger.exception("Cannot send Matrix message to %s due to verification error: %s; device %s", room_id, err, err.device)
                raise control.MessageSendError(f"Matrix verification failed: {err}") from err
            except asyncio.TimeoutError as exn:
                logger.warning("Matrix delivery to room %s timed out after %s seconds; message %s",
                               room_id, self.send_timeout, message, exc_info=True)
                raise control.MessageSendError(f"Matrix delivery timed out after {self.send_timeout} seconds for {room_id}") from exn
            except aiohttp.ClientError as exn:
                raise control.MessageSendError(f"Matrix delivery unavailable: {exn}") from exn

    async def _invite_callback(self, room: MatrixRoom, event: Event) -> None:
        assert isinstance(event, InviteEvent)
        if self._admin_room_id is None:
            logger.debug("invite callback to %s event %s: joining to admin room", room, event)
            await self._client.join(room.room_id)
            self._admin_room_id = room.room_id
            await self._state.save()
            logger.info("Room %s is encrypted: %s", room.name, room.encrypted)
            if self._init_done.is_set():
                await self.send_message(control.MessageContext(admin_room=True), "This is the admin room. Invite to another room or use !sameroom to set this to be the control room as well.")
        elif self._room_id is None:
            logger.debug("invite callback to %s event %s: joining to control room", room, event)
            await self._client.join(room.room_id)
            self._room_id = room.room_id
            await self._state.save()
            logger.info("Room %s is encrypted: %s", room.name, room.encrypted)
            if self._init_done.is_set():
                await self.send_message(control.MessageContext(admin_room=False), "This is the control room.")
        else:
            logger.debug("invite callback to %s event %s: not joining, we are already in %s", room, event, self._room_id)

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
            logger.info("Matrix ready")
            for pending in self._pending_event_handlers:
                await pending()
            self._pending_event_handlers = []
        # https://matrix-nio.readthedocs.io/en/latest/examples.html?highlight=invite#manual-encryption-key-verification
        after_first_sync_task = asyncio.ensure_future(after_first_sync())
        sync_forever_task = asyncio.ensure_future(self._client.sync_forever(timeout=30000, since=self._sync_token, full_state=True))
        logger.info(f"Sync starts")
        try:
            done, _ = await asyncio.wait([after_first_sync_task, sync_forever_task], return_when=asyncio.FIRST_COMPLETED)
            if after_first_sync_task in done:
                await after_first_sync_task
                await sync_forever_task
            else:
                await sync_forever_task
                raise control.ControlException("Matrix sync returned unexpectedly")
        finally:
            after_first_sync_task.cancel()
            sync_forever_task.cancel()
            await asyncio.gather(after_first_sync_task, sync_forever_task, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await self._join(self._close_task)

    async def _close(self) -> None:
        tasks = list(self._send_tasks)
        for cancellation in self._send_tasks.values():
            cancellation.set()
        if tasks:
            await asyncio.wait(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._send_tasks.pop(task, None)
        await self._client.close()
