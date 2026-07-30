import asyncio
import itertools
import uuid
from datetime import datetime
from typing import Optional

# Priority scale used across the network: 1 = critical, 5 = background.
CRITICAL_PRIORITY = 1
DEFAULT_PRIORITY = 3
LOW_PRIORITY = 5


class Message:
    def __init__(self, from_agent: str, to_agent: str, msg_type: str,
                 payload: dict, priority: int = DEFAULT_PRIORITY,
                 correlation_id: str = None):
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

    def __repr__(self):
        return (f"Message({self.type} {self.from_agent}->{self.to_agent} "
                f"p{self.priority})")


class MessageBus:
    """Async priority queue with an append-only history of everything published."""

    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._history: list[dict] = []
        # Tie-break on insertion order so equal priorities stay FIFO and the
        # heap never has to compare two Message objects.
        self._sequence = itertools.count()

    async def publish(self, message: Message) -> None:
        await self._queue.put((message.priority, next(self._sequence), message))
        self._history.append(message.to_dict())

    def publish_nowait(self, message: Message) -> None:
        """Publish without awaiting — safe to call from synchronous code."""
        self._queue.put_nowait((message.priority, next(self._sequence), message))
        self._history.append(message.to_dict())

    async def consume(self, timeout: float = 5.0) -> Optional[Message]:
        try:
            _, _, msg = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            return msg
        except asyncio.TimeoutError:
            return None

    def get_history(self) -> list[dict]:
        return self._history.copy()

    def history_for(self, correlation_id: str) -> list[dict]:
        """Every message belonging to one task exchange, in publication order."""
        return [m for m in self._history if m["correlation_id"] == correlation_id]

    def size(self) -> int:
        return self._queue.qsize()
