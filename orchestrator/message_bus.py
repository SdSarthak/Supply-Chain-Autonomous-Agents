import asyncio
import uuid
from datetime import datetime
from typing import Optional


class Message:
    def __init__(self, from_agent: str, to_agent: str, msg_type: str,
                 payload: dict, priority: int = 3, correlation_id: str = None):
        self.msg_id = str(uuid.uuid4())
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.type = msg_type
        self.payload = payload
        self.priority = priority  # 1=critical, 5=low
        self.timestamp = datetime.utcnow().isoformat()
        self.correlation_id = correlation_id or self.msg_id

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "type": self.type,
            "payload": self.payload,
            "priority": self.priority,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }

    def __lt__(self, other):
        return self.priority < other.priority


class MessageBus:
    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._history: list[dict] = []

    async def publish(self, message: Message) -> None:
        await self._queue.put((message.priority, message))
        self._history.append(message.to_dict())

    async def consume(self, timeout: float = 5.0) -> Optional[Message]:
        try:
            _, msg = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            return msg
        except asyncio.TimeoutError:
            return None

    def publish_sync(self, message: Message) -> None:
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.publish(message))
        except RuntimeError:
            # If no running loop, store directly
            self._history.append(message.to_dict())

    def get_history(self) -> list[dict]:
        return self._history.copy()

    def size(self) -> int:
        return self._queue.qsize()
