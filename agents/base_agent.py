import json
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional
import google.generativeai as genai
import google.ai.generativelanguage as glm
from google.generativeai.types import Tool

from config import GEMINI_API_KEY, AGENT_CHAT_HISTORY_MAX
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory

logger = logging.getLogger(__name__)


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
    ):
        self.name = name
        self.model_name = model
        self.system_prompt = system_prompt
        self._tools = tools
        self._tool_declarations = tool_declarations
        self.redis = redis_mem
        self.sqlite = sqlite_mem

        genai.configure(api_key=GEMINI_API_KEY)
        gemini_tools = [Tool(function_declarations=tool_declarations)] if tool_declarations else []

        self._model = genai.GenerativeModel(
            model_name=model,
            system_instruction=system_prompt,
            tools=gemini_tools,
        )

    def _get_chat_history(self) -> list[dict]:
        return self.redis.get_list(RedisMemory.agent_history_key(self.name))

    def _save_turn(self, role: str, content: str) -> None:
        self.redis.append_to_list(
            RedisMemory.agent_history_key(self.name),
            {"role": role, "parts": [{"text": content}]},
            max_len=AGENT_CHAT_HISTORY_MAX
        )

    def _call_gemini(self, prompt: str, max_tool_rounds: int = 8) -> str:
        history = self._get_chat_history()
        chat = self._model.start_chat(history=history)
        self._save_turn("user", prompt)

        response = chat.send_message(prompt)

        for _ in range(max_tool_rounds):
            candidate = response.candidates[0]
            parts = candidate.content.parts

            has_function_call = any(hasattr(p, "function_call") and p.function_call.name for p in parts)
            if not has_function_call:
                break

            tool_results = []
            for part in parts:
                if hasattr(part, "function_call") and part.function_call.name:
                    fc = part.function_call
                    result = self._dispatch_tool(fc.name, dict(fc.args))
                    logger.debug(f"[{self.name}] Tool {fc.name}({dict(fc.args)}) -> {str(result)[:200]}")
                    tool_results.append({
                        "function_response": {
                            "name": fc.name,
                            "response": result
                        }
                    })

            response = chat.send_message(tool_results)

        final_text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                final_text += part.text

        self._save_turn("model", final_text)
        return final_text

    def _dispatch_tool(self, name: str, args: dict) -> Any:
        fn = self._tools.get(name)
        if fn is None:
            return {"error": f"Tool '{name}' not registered on agent '{self.name}'"}
        try:
            return fn(**args)
        except Exception as e:
            logger.error(f"[{self.name}] Tool {name} raised: {e}")
            return {"error": str(e)}

    def save_state(self, state: dict) -> None:
        self.redis.set(RedisMemory.agent_state_key(self.name), state)

    def load_state(self) -> Optional[dict]:
        return self.redis.get(RedisMemory.agent_state_key(self.name))

    def clear_history(self) -> None:
        self.redis.clear_list(RedisMemory.agent_history_key(self.name))

    @abstractmethod
    def run(self, task: dict) -> dict:
        pass

    def _log(self, msg: str) -> None:
        logger.info(f"[{self.name}] {msg}")
        print(f"  [{self.name}] {msg}")
