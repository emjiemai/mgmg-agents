"""LLM client — OpenRouter, any model by one setting, with a fallback chain.

The model only writes text. It gets no tools, cannot change data and never decides access:
everything it sees was already filtered by the server (spec §20, §23).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx
from loguru import logger

from hari.config import settings

URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMUnavailable(Exception):
    """No key, provider down, limit reached or every model failed (A12: HARI keeps working without AI)."""


@dataclass
class LLMReply:
    data: dict
    model: str


class LLMClient:
    def __init__(self, api_key: str | None = None, models: list[str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key.get_secret_value()
        self.models = models or settings.llm_models
        self._transport = transport

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> LLMReply:
        if not self.api_key.strip():
            raise LLMUnavailable("no API key")
        last_error = "unknown"
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds, transport=self._transport) as client:
            for model in self.models:
                try:
                    resp = await client.post(
                        URL,
                        headers={"Authorization": f"Bearer {self.api_key}", "X-Title": "HARI"},
                        json={
                            "model": model,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            "response_format": {"type": "json_object"},
                            "temperature": 0.1,
                            "max_tokens": max_tokens,
                        },
                    )
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}"
                        logger.warning("LLM {} failed: {}", model, last_error)
                        continue
                    content = resp.json()["choices"][0]["message"].get("content") or ""
                    return LLMReply(data=parse_json(content), model=model)
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                    last_error = type(exc).__name__
                    logger.warning("LLM {} failed: {}", model, last_error)
        raise LLMUnavailable(last_error)


def parse_json(content: str) -> dict:
    """Models sometimes wrap JSON in a code fence; take the object either way."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data
