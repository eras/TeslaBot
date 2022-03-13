import os
from typing import List, Union, Optional, Any
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

    def save(self, state: ConfigParser) -> None:
        if not "slack" in state:
            state["slack"] = {}
        st = state["slack"]
        st["channel_id"]= get_optional(self.control._channel_id, "")

class SlackControl(control.Control):
    _client: WebClient
    # _ws: websocket.WebSocketApp
    _config: Config
    _state: State
    _client_id: str
    _client_secret: str
    _channel_name: str
    _channel_id: Optional[str]
    _local_commands: commands.Commands[CommandContext]
    _signing_secret: str
    _api_token: str
    _app_token: str
    _ws_task: Any               # async_io.Task[Any] won't work with Python..
    _aiohttp_session: aiohttp.ClientSession

    def __init__(self, env: Env) -> None:
        super().__init__()
        self._config = env.config
        self._state = env.state
        signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
        if signing_secret is None:
            signing_secret = self._config.config["slack"]["signing_secret"]
        api_token = os.environ.get("SLACK_API_TOKEN")
        if api_token is None:
            api_token = self._config.config["slack"]["api_token"]
        app_token = os.environ.get("SLACK_APP_TOKEN")
        if app_token is None:
            app_token = self._config.config["slack"]["app_token"]
        self._signing_secret = signing_secret
        self._api_token = api_token
        self._app_token = app_token
        self._client_id = self._config.config["slack"]["client_id"]
        self._client_secret = self._config.config["slack"]["client_secret"]
        channel_name = self._config.config.get("slack", "channel", fallback=None)
        if channel_name is None or channel_name == "":
            raise control.ConfigError("Missing slack.channel configuration")
        if channel_name[0] != "#":
            raise control.ConfigError("Expected channel name to start with #")
        self._channel_name = channel_name
        self._channel_id = self._state.state.get("slack", "channel_id", fallback=None)
        self._client = WebClient(token=api_token, run_async=True)
        self._local_commands = commands.Commands()
        self._local_commands.register(commands.Function[CommandContext]("ping", self._command_ping))
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
                    self._state.save()
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
                            text = json_message.get("payload", {}).get("event", {}).get("text", None)
                            if text is not None and text[0] == "!":
                                logger.info(f"< {text}")
                                invocation = commands.Invocation.parse(text[1:])
                                command_context = CommandContext(admin_room=False)
                                if self._local_commands.has_command(invocation.name):
                                    await self._local_commands.invoke(command_context, invocation)
                                else:
                                    await self.callback.command_callback(command_context, invocation)
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

    async def _command_ping(self, context: CommandContext, args: List[str]) -> None:
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