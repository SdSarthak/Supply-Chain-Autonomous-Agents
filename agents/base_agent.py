import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

from config import (GEMINI_API_KEY, AGENT_CHAT_HISTORY_MAX, MAX_TOOL_ROUNDS,
                    GEMINI_MAX_RETRIES, GEMINI_RETRY_BASE_DELAY, OFFLINE_MODE)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory

logger = logging.getLogger(__name__)

# Substrings that mark a Gemini error as worth retrying rather than aborting.
RETRYABLE_ERROR_MARKERS = (
    "429", "500", "502", "503", "504",
    "rate limit", "resource has been exhausted", "resourceexhausted",
    "deadline exceeded", "service unavailable", "internal error", "overloaded",
)

FINAL_ANSWER_NUDGE = (
    "You have used the tool budget for this task. Stop calling tools and reply "
    "now with the final JSON object requested, using the data you already have."
)


def extract_json(text: str) -> dict:
    """Pull the first complete JSON object out of a model response.

    Handles bare JSON, ```json fenced blocks, and JSON embedded in prose. Uses
    brace matching rather than a greedy regex so trailing commentary after the
    object does not break parsing. Returns {} when nothing parses.
    """
    if not text:
        return {}

    candidate = text.strip()
    if candidate.startswith("```"):
        for block in candidate.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{"):
                candidate = block
                break

    start = candidate.find("{")
    if start == -1:
        return {}

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(candidate)):
        char = candidate[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(candidate[start:idx + 1])
                except json.JSONDecodeError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}


def is_retryable(error: Exception) -> bool:
    message = f"{type(error).__name__} {error}".lower()
    return any(marker in message for marker in RETRYABLE_ERROR_MARKERS)


class BaseAgent(ABC):
    def __init__(
        self,
        name: str,
        model: str,
        system_prompt: str,
        tools: dict[str, Callable],
        tool_declarations: list,
        redis_mem: RedisMemory,
        sqlite_mem: SQLiteMemory,
        offline: Optional[bool] = None,
    ):
        self.name = name
        self.model_name = model
        self.system_prompt = system_prompt
        self._tools = tools
        self._tool_declarations = tool_declarations
        self.redis = redis_mem
        self.sqlite = sqlite_mem
        # Set by the orchestrator when the run must keep stdout clean (--json).
        self.quiet = False

        if offline is None:
            offline = OFFLINE_MODE or not GEMINI_API_KEY
        self.offline = offline
        self._model = None
        if not self.offline:
            self._model = self._build_model()

    def _build_model(self):
        # Imported lazily so offline runs never touch the Gemini SDK.
        import google.generativeai as genai
        from google.generativeai.types import Tool

        genai.configure(api_key=GEMINI_API_KEY)
        gemini_tools = [Tool(function_declarations=self._tool_declarations)] \
            if self._tool_declarations else []
        return genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=self.system_prompt,
            tools=gemini_tools,
        )

    # ── chat history ─────────────────────────────────────────
    def _get_chat_history(self) -> list[dict]:
        return self._sanitize_history(
            self.redis.get_list(RedisMemory.agent_history_key(self.name))
        )

    @staticmethod
    def _sanitize_history(history: list[dict]) -> list[dict]:
        """Coerce stored turns into a shape Gemini will accept.

        The API requires history to start with a user turn, alternate roles and
        carry no empty parts. A run that died mid-turn can leave a dangling user
        message behind, so anything malformed is dropped rather than poisoning
        every later call.
        """
        clean: list[dict] = []
        for turn in history:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            parts = turn.get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if role not in ("user", "model") or not text.strip():
                continue
            if not clean and role != "user":
                continue
            if clean and clean[-1]["role"] == role:
                clean[-1] = {"role": role, "parts": [{"text": text}]}
                continue
            clean.append({"role": role, "parts": [{"text": text}]})
        # A trailing user turn would look like an unanswered prompt.
        if clean and clean[-1]["role"] == "user":
            clean.pop()
        return clean

    def _save_turn(self, role: str, content: str) -> None:
        if not content or not content.strip():
            return
        self.redis.append_to_list(
            RedisMemory.agent_history_key(self.name),
            {"role": role, "parts": [{"text": content}]},
            max_len=AGENT_CHAT_HISTORY_MAX
        )

    # ── Gemini loop ──────────────────────────────────────────
    def _send(self, chat, message):
        """Send one message, retrying transient API failures with backoff."""
        last_error = None
        for attempt in range(GEMINI_MAX_RETRIES + 1):
            try:
                return chat.send_message(message)
            except Exception as e:  # the SDK raises a family of transport errors
                last_error = e
                if attempt >= GEMINI_MAX_RETRIES or not is_retryable(e):
                    raise
                delay = GEMINI_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("[%s] Gemini call failed (%s), retrying in %.1fs",
                               self.name, e, delay)
                time.sleep(delay)
        raise last_error

    def _call_gemini(self, prompt: str, max_tool_rounds: int = MAX_TOOL_ROUNDS) -> str:
        chat = self._model.start_chat(history=self._get_chat_history())

        response = self._send(chat, prompt)
        tool_calls_made = 0

        for _ in range(max_tool_rounds):
            parts = self._response_parts(response)
            calls = [p.function_call for p in parts
                     if getattr(p, "function_call", None) and p.function_call.name]
            if not calls:
                break

            tool_results = []
            for fc in calls:
                args = dict(fc.args)
                result = self._dispatch_tool(fc.name, args)
                tool_calls_made += 1
                logger.debug("[%s] Tool %s(%s) -> %s", self.name, fc.name, args, str(result)[:200])
                tool_results.append({
                    "function_response": {"name": fc.name, "response": result}
                })

            response = self._send(chat, tool_results)

        final_text = self._response_text(response)
        if not final_text:
            # Tool budget exhausted (or an empty turn) — ask once for the answer.
            final_text = self._response_text(self._send(chat, FINAL_ANSWER_NUDGE))

        if final_text:
            self._save_turn("user", prompt)
            self._save_turn("model", final_text)
        logger.info("[%s] Gemini turn complete: %d tool calls, %d chars",
                    self.name, tool_calls_made, len(final_text))
        return final_text

    @staticmethod
    def _response_parts(response) -> list:
        """Content parts of the first candidate, or [] if there is no usable one.

        A response can legitimately carry no candidate at all — safety blocks,
        recitation stops and quota-truncated turns all return an empty
        `candidates` list — so indexing it directly raises IndexError mid-cycle.
        Callers treat "no parts" as "no tool calls and no text", which routes the
        agent to its deterministic engine instead of killing the step.
        """
        try:
            return list(response.candidates[0].content.parts)
        except (AttributeError, IndexError, TypeError):
            return []

    @classmethod
    def _response_text(cls, response) -> str:
        parts = cls._response_parts(response)
        return "".join(p.text for p in parts if getattr(p, "text", "")).strip()

    def _dispatch_tool(self, name: str, args: dict) -> dict:
        fn = self._tools.get(name)
        if fn is None:
            return {"error": f"Tool '{name}' is not registered on agent '{self.name}'"}
        try:
            result = fn(**args)
        except TypeError as e:
            return {"error": f"Bad arguments for '{name}': {e}"}
        except Exception as e:
            logger.error("[%s] Tool %s raised: %s", self.name, name, e)
            return {"error": str(e)}
        # A function_response payload must be a JSON object, not a bare list.
        return result if isinstance(result, dict) else {"result": result}

    # ── reasoning entry point ────────────────────────────────
    def _reason(self, prompt: str, task: dict) -> tuple[dict, str]:
        """Produce this agent's structured result.

        Online: Gemini plans, calls tools and returns JSON. Offline (or when the
        model returns nothing parsable): the agent's deterministic engine
        computes the same shape from the same tools. Both paths return
        (parsed_result, raw_text).
        """
        if self.offline:
            result = self._offline_result(task)
            return result, json.dumps(result, indent=2)

        try:
            raw = self._call_gemini(prompt)
        except Exception as e:
            # An exhausted retry budget, a revoked key or a network outage must
            # not take the cycle down: every agent carries a deterministic
            # engine precisely so the step can still produce a real answer.
            logger.exception("[%s] Gemini call failed, falling back to the "
                             "deterministic engine", self.name)
            self._log(f"Gemini unavailable ({type(e).__name__}: {e}) — "
                      f"using deterministic fallback.")
            return self._offline_result(task), f"gemini_error: {type(e).__name__}: {e}"

        parsed = extract_json(raw)
        if not parsed:
            self._log("Model returned no parsable JSON — using deterministic fallback.")
            return self._offline_result(task), raw
        return parsed, raw

    def save_state(self, state: dict) -> None:
        self.redis.set(RedisMemory.agent_state_key(self.name), state)

    def load_state(self) -> Optional[dict]:
        return self.redis.get(RedisMemory.agent_state_key(self.name))

    def clear_history(self) -> None:
        self.redis.clear_list(RedisMemory.agent_history_key(self.name))

    @abstractmethod
    def run(self, task: dict) -> dict:
        """Execute this agent's step of the cycle and persist its output."""

    @abstractmethod
    def _offline_result(self, task: dict) -> dict:
        """Deterministic implementation of this agent's decision logic."""

    def _log(self, msg: str) -> None:
        logger.info("[%s] %s", self.name, msg)
        if not self.quiet:
            print(f"  [{self.name}] {msg}")
