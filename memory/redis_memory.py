import fnmatch
import json
import logging
import threading
import time
import redis
from typing import Any, Optional
from config import REDIS_HOST, REDIS_PORT, REDIS_DB, AGENT_CHAT_HISTORY_MAX

logger = logging.getLogger(__name__)


class InMemoryStore:
    """Thread-safe stand-in for the subset of Redis this project uses.

    Lets the whole cycle run (offline demos, CI, unit tests) on a machine with
    no Redis server. It speaks the same method names as redis.Redis so
    RedisMemory can hold either without branching.
    """

    def __init__(self):
        self._values: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}
        self._lock = threading.RLock()

    # ── internals ────────────────────────────────────────────
    def _expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self._values.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    def _read(self, key: str, expected_type: type, default):
        if self._expired(key):
            return default
        value = self._values.get(key, default)
        if value is not default and not isinstance(value, expected_type):
            raise TypeError(
                f"Key '{key}' holds {type(value).__name__}, expected {expected_type.__name__}"
            )
        return value

    # ── string ops ───────────────────────────────────────────
    def ping(self) -> bool:
        return True

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._values[key] = value
            self._expiry.pop(key, None)

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            self._values[key] = value
            self._expiry[key] = time.monotonic() + ttl

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._read(key, str, None)

    def delete(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._values.pop(key, None)
                self._expiry.pop(key, None)

    def expire(self, key: str, ttl: int) -> None:
        with self._lock:
            if key in self._values:
                self._expiry[key] = time.monotonic() + ttl

    # ── list ops ─────────────────────────────────────────────
    def rpush(self, key: str, value: str) -> None:
        with self._lock:
            items = self._read(key, list, None)
            if items is None:
                items = []
                self._values[key] = items
            items.append(value)

    def ltrim(self, key: str, start: int, end: int) -> None:
        with self._lock:
            items = self._read(key, list, None)
            if items is None:
                return
            self._values[key] = items[start:] if end == -1 else items[start:end + 1]

    def lrange(self, key: str, start: int, end: int) -> list:
        with self._lock:
            items = self._read(key, list, [])
            if end == -1:
                return list(items[start:])
            return list(items[start:end + 1])

    # ── hash ops ─────────────────────────────────────────────
    def hset(self, key: str, field: str = None, value: str = None,
             mapping: dict = None) -> None:
        with self._lock:
            bucket = self._read(key, dict, None)
            if bucket is None:
                bucket = {}
                self._values[key] = bucket
            if mapping:
                bucket.update(mapping)
            if field is not None:
                bucket[field] = value

    def hgetall(self, key: str) -> dict:
        with self._lock:
            return dict(self._read(key, dict, {}))

    def hget(self, key: str, field: str) -> Optional[str]:
        with self._lock:
            return self._read(key, dict, {}).get(field)

    def keys(self, pattern: str = "*") -> list:
        with self._lock:
            live = [k for k in list(self._values) if not self._expired(k)]
            return fnmatch.filter(live, pattern)


class RedisMemory:
    """JSON-serialising wrapper over Redis (or the in-memory fallback)."""

    def __init__(self, allow_fallback: bool = False, client: Any = None):
        self.using_fallback = False
        if client is not None:
            self._client = client
            self.using_fallback = not isinstance(client, redis.Redis)
            return

        self._client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            decode_responses=True, socket_connect_timeout=5
        )
        if allow_fallback and not self.ping():
            logger.warning("Redis unreachable at %s:%s — falling back to in-memory store",
                           REDIS_HOST, REDIS_PORT)
            self._client = InMemoryStore()
            self.using_fallback = True

    @property
    def backend(self) -> str:
        return "in-memory" if self.using_fallback else "redis"

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
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
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def append_to_list(self, key: str, value: Any, max_len: int = AGENT_CHAT_HISTORY_MAX) -> None:
        self._client.rpush(key, json.dumps(value))
        self._client.ltrim(key, -max_len, -1)

    def get_list(self, key: str) -> list:
        decoded = []
        for item in self._client.lrange(key, 0, -1):
            try:
                decoded.append(json.loads(item))
            except (TypeError, ValueError):
                continue
        return decoded

    def clear_list(self, key: str) -> None:
        self._client.delete(key)

    def set_hash(self, key: str, mapping: dict, ttl: Optional[int] = None) -> None:
        self._client.hset(key, mapping={k: json.dumps(v) for k, v in mapping.items()})
        if ttl:
            self._client.expire(key, ttl)

    def get_hash(self, key: str) -> dict:
        decoded = {}
        for k, v in self._client.hgetall(key).items():
            try:
                decoded[k] = json.loads(v)
            except (TypeError, ValueError):
                decoded[k] = v
        return decoded

    def hset(self, key: str, field: str, value: Any) -> None:
        self._client.hset(key, field, json.dumps(value))

    def hget(self, key: str, field: str) -> Optional[Any]:
        raw = self._client.hget(key, field)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

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
