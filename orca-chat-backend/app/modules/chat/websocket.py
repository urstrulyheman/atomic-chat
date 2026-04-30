from uuid import UUID

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder


class ConnectionManager:
    def __init__(self) -> None:
        self.active: dict[UUID, list[WebSocket]] = {}

    async def connect(self, user_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.setdefault(user_id, []).append(websocket)

    def disconnect(self, user_id: UUID, websocket: WebSocket) -> None:
        sockets = self.active.get(user_id, [])
        if websocket in sockets:
            sockets.remove(websocket)
        if not sockets and user_id in self.active:
            del self.active[user_id]

    async def send_user(self, user_id: UUID, payload: dict) -> None:
        for socket in self.active.get(user_id, []):
            await socket.send_json(jsonable_encoder(payload))


manager = ConnectionManager()
