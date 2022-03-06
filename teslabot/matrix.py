import asyncio
import re

from nio import AsyncClient, MatrixRoom, RoomMessageText

from . import control

class MatrixControl(control.Control):
    client: AsyncClient
    room_id: str

    def __init__(self) -> None:
        super().__init__()
        self.client = AsyncClient("https://matrix.example.org", "@alice:example.org")
        self.client.add_event_callback(self._message_callback, RoomMessageText)

    async def setup(self) -> None:
        await self.client.login("my-secret-password")

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
