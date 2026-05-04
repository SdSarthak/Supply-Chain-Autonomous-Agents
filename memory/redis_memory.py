import json
import redis
from typing import Any, Optional
from config import REDIS_HOST, REDIS_PORT, REDIS_DB, AGENT_CHAT_HISTORY_MAX


class RedisMemory:
    def __init__(self):
        self._client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            decode_responses=True, socket_connect_timeout=5
        )

    def ping(self) -> bool:
        try:
            return self._client.ping()
        except Exception:
            return False

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        serialized = json.dumps(value)
        if ttl:
            self._client.setex(key, ttl, serialized)
        else:
            self._client.set(key, serialized)

    def get(self, key: str) -> Optional[Any]:
        raw = self._client.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def append_to_list(self, key: str, value: Any, max_len: int = AGENT_CHAT_HISTORY_MAX) -> None:
        self._client.rpush(key, json.dumps(value))
        self._client.ltrim(key, -max_len, -1)

    def get_list(self, key: str) -> list:
        items = self._client.lrange(key, 0, -1)
        return [json.loads(i) for i in items]

    def clear_list(self, key: str) -> None:
        self._client.delete(key)

    def set_hash(self, key: str, mapping: dict, ttl: Optional[int] = None) -> None:
        self._client.hset(key, mapping={k: json.dumps(v) for k, v in mapping.items()})
        if ttl:
            self._client.expire(key, ttl)

    def get_hash(self, key: str) -> dict:
        raw = self._client.hgetall(key)
        return {k: json.loads(v) for k, v in raw.items()}

    def hset(self, key: str, field: str, value: Any) -> None:
        self._client.hset(key, field, json.dumps(value))

    def hget(self, key: str, field: str) -> Optional[Any]:
        raw = self._client.hget(key, field)
        return json.loads(raw) if raw else None

    # Convenience key builders
    @staticmethod
    def agent_state_key(agent_name: str) -> str:
        return f"agent:{agent_name}:state"

    @staticmethod
    def agent_history_key(agent_name: str) -> str:
        return f"agent:{agent_name}:chat_history"

    @staticmethod
    def inventory_key(sku_id: str) -> str:
        return f"inventory:live:{sku_id}"

    @staticmethod
    def negotiation_key(session_id: str) -> str:
        return f"negotiation:{session_id}"

    @staticmethod
    def cycle_state_key() -> str:
        return "orchestrator:cycle_state"
