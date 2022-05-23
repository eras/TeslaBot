import os
from typing import List, Union, Optional, Any, Tuple
import asyncio
import aiohttp
import json
from configparser import ConfigParser

# import websocket

from slack.web.client import WebClient
from slack.signature.verifier import SignatureVerifier
from slack.web.slack_response import SlackResponse
from slack.errors import SlackApiError

from . import log
from . import control
from .control import CommandContext, MessageContext
from .env import Env
from .config import Config
from . import commands
from . import parser
from .state import State, StateElement
from .utils import get_optional

def assert_future(x: Union[asyncio.Future, SlackResponse]) -> asyncio.Future: # type: ignore
    assert isinstance(x, asyncio.Future)
    return x

logger = log.getLogger(__name__)

class StateSave(StateElement):
    control: "SlackControl"

    def __init__(self, control: "SlackControl") -> None:
        self.control = control

    async def save(self, state: State) -> None:
        if state.has_section("slack"):
            state["slack"] = {}
        st = state["slack"]
        st["channel_id"]= get_optional(self.control._channel_id, "")

class SlackControl(control.Control):
    _client: WebClient
    # _ws: websocket.WebSocketApp
    _config: Config
    _state: State
    _channel_name: str
    _channel_id: Optional[str]
    _admin_channel_id: str

    _api_token: str
    _app_token: str
    _ws_task: Any               # async_io.Task[Any] won't work with Python..
    _aiohttp_session: aiohttp.ClientSession

    def __init__(self, env: Env) -> None:
        super().__init__()
        self._config = env.config
        self._state = env.state
        api_token = os.environ.get("SLACK_API_TOKEN")
        if api_token is None:
            api_token = self._config.get("slack", "slack_api_secret_id")
        app_token = os.environ.get("SLACK_APP_TOKEN")
        if app_token is None:
            app_token = self._config.get("slack", "slack_app_secret_id")
        admin_channel_id = os.environ.get("SLACK_ADMIN_CHANNEL_ID")
        if admin_channel_id is None:
            admin_channel_id = self._config.get("slack", "slack_admin_channel_id")
        self._api_token = api_token
        self._app_token = app_token
        channel_name = self._config.get("slack", "channel", empty_is_none=True)
        if channel_name[0] != "#":
            raise control.ConfigError("Expected channel name to start with #")
        self._channel_name = channel_name
        self._channel_id = self._state.get("slack", "channel_id", fallback=None)
        self._client = WebClient(token=api_token, run_async=True)
        self._aiohttp_session = aiohttp.ClientSession()

    async def setup(self) -> None:
        if self._channel_id is None:
            result = await assert_future(self._client.api_call(
                api_method="users.conversations",
                json={}
            ))
            if result["ok"]:
                logger.debug(f"result: {result}")
                ids = [channel["id"] for channel in result["channels"] if f"#{channel['name']}" == self._channel_name]
                if ids:
                    self._channel_id = ids[0]
                    await self._state.save()
                else:
                    raise control.ConfigError(f"Could not find channel {self._channel_name} from the list of joined conversations")
        # logger.info("Post message")
        # await assert_future(self._client.api_call(
        #     api_method="chat.postMessage",
        #     json={"channel": self._channel_id,
        #           "text": "hello world"}
        # ))
        self._ws_task = asyncio.create_task(self._ws_handler())

    async def run(self) -> None:
        pass

    async def _ws_handler(self) -> None:
        num_retries = 0
        def sleep_time() -> float:
            return min(120, pow(1.15, num_retries) * 10)
        try:
            while True:
                ws_url: Optional[str] = None
                async with self._aiohttp_session.post("https://slack.com/api/apps.connections.open",
                                                      headers={"Authorization": f"Bearer {self._app_token}",
                                                               "Content-type": "application/x-www-form-urlencoded"}) as response:
                    if response.status == 200:
                        data = json.loads(await response.text())
                        if bool(data.get("ok")):
                            ws_url = data["url"]

                if ws_url is None:
                    logger.error(f"Failed to acquire web socket URL; sleeping {sleep_time()} seconds and trying again")
                    await asyncio.sleep(sleep_time())
                    num_retries += 1
                else:
                    got_messages = False
                    async with aiohttp.ClientSession().ws_connect(ws_url) as session:
                        logger.debug(f"Established websocket connection, waiting first message..")
                        async for message in session:
                            if not got_messages:
                                logger.info(f"Established websocket connection successfully")
                            got_messages = True
                            json_message = json.loads(message.data)
                            logger.info(f"json_message: {json_message}")
                            try:
                                envelope_id = json_message.get("envelope_id")
                            except Exception as exn:
                                logger.error(f"exception1: {exn}")
                                raise exn
                            # ack first, handle later, so we don't end up reprocessing crashing commands..
                            if envelope_id is not None:
                                ack = {"envelope_id": envelope_id}
                                logger.debug(f"acking with {ack}")
                                await session.send_json(ack)
                                logger.debug(f"acked")
                            # Filter through bot messages and set admin rights
                            text = json_message.get("payload", {}).get("event", {}).get("text", None)
                            bot = json_message.get("payload", {}).get("event", {}).get("bot_id", None)
                            if text is not None and bot is None:
                                admin_room = False if json_message.get("payload", {}).get("event", {}).get("channel", None) is not self._admin_channel_id else True
                                command_context = CommandContext(admin_room=admin_room,
                                                                 control=self)
                                await self.process_message(command_context, text)
                            if bot is not None:
                                logger.debug(f"Not processing bot messages as commands")
                    if got_messages:
                        logger.error(f"Web socket session terminated: sleeping 10 seconds and reconnecting")
                        await asyncio.sleep(10)
                        num_retries = 0
                    else:
                        logger.error(f"Web socket session terminated without receiving any data: sleeping {sleep_time()} seconds and reconnecting")
                        await asyncio.sleep(sleep_time())
                        num_retries += 1
        except Exception as exn:
            logger.error(f"exception: {exn}")
            raise exn

    async def _command_ping(self, context: CommandContext, valid: Tuple[()]) -> None:
        await self.send_message(context.to_message_context(), "pong")

    async def send_message(self,
                           message_context: control.MessageContext,
                           message: str) -> None:
        assert len(message) == 0 or message[0] != "!"
        try:
            response = await assert_future(self._client.api_call(
                api_method="chat.postMessage",
                json={"channel": self._channel_id,
                      "text": message}
            ))
            if response["message"]["text"] != message:
                raise control.MessageSendError("Sent message different from requested")
        except SlackApiError as exn:
            assert exn.response["ok"] is False
            error = exn.response["error"] # str like 'invalid_auth', 'channel_not_found'
            raise control.MessageSendError(error) from exn
        pass
