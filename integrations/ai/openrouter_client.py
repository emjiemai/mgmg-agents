"""OpenRouter client — used by the Lead Agent to qualify raw search hits.

Verified against openrouter.ai/docs/quickstart on 2026-08-19: OpenAI-compatible
``chat/completions`` endpoint, ``Authorization: Bearer`` auth, reply text at
``choices[0].message.content``.

Model is a plain config value (``OPENROUTER_MODEL``, default
``google/gemini-3.7-flash``) — swapping models later is a one-line env change,
no code touched.
"""

from __future__ import annotations

import json
import uuid

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("openrouter")

BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter returns an error the client cannot recover from."""


class OpenRouterClient:
    """Async client for OpenRouter chat completions.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OpenRouterClient":
        """Open the HTTP client with the bearer token attached.

        Returns:
            The ready client.

        Raises:
            OpenRouterError: if the API key is unset.
        """
        key = settings.openrouter_api_key.get_secret_value()
        if not key:
            raise OpenRouterError("OpenRouter is not configured — fill OPENROUTER_API_KEY in .env")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://mgmg-command-center.internal",
                "X-Title": "MGMG Lead Agent",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        """Run one chat completion.

        Args:
            system: System prompt.
            user: User message.
            json_mode: Request a JSON-only response (OpenRouter passes this
                through to providers that support structured output; not all
                do, so callers should still parse defensively).

        Returns:
            The model's reply text.

        Raises:
            OpenRouterError: on a non-200 response or an empty completion.
        """
        assert self._client is not None
        payload: dict = {
            "model": settings.openrouter_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="openrouter",
            run_id=self.run_id,
            target_ref=settings.openrouter_model,
            payload={"prompt_chars": len(system) + len(user)},
        ) as ctx:
            # request_with_retry raises the raw httpx exception once retries
            # are exhausted (e.g. a 429 that never clears) rather than
            # returning a response — that must still surface as OpenRouterError
            # so callers catching our typed error (not just Exception) degrade
            # gracefully instead of crashing the whole agent run.
            try:
                response = await request_with_retry(self._client, "POST", BASE_URL, json=payload)
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                ctx["http_status"] = getattr(getattr(exc, "response", None), "status_code", None)
                raise OpenRouterError(f"OpenRouter completion failed after retries: {exc}") from exc

            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise OpenRouterError(
                    f"OpenRouter completion failed: HTTP {response.status_code} {response.text[:300]}"
                )
            body = response.json()
            choices = body.get("choices", [])
            if not choices:
                raise OpenRouterError(f"OpenRouter returned no choices: {body}")
            text = choices[0].get("message", {}).get("content", "")
            ctx["payload"]["completion_chars"] = len(text)

        return text

    async def complete_json(self, system: str, user: str) -> dict:
        """Run a completion and parse the reply as JSON.

        Args:
            system: System prompt (should instruct the model to return JSON).
            user: User message.

        Returns:
            The parsed JSON object.

        Raises:
            OpenRouterError: on a non-200 response, empty completion, or a
                reply that isn't valid JSON (fenced code blocks are stripped
                first, since some models wrap JSON in ```json anyway).
        """
        text = await self.complete(system, user, json_mode=True)
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise OpenRouterError(f"OpenRouter reply was not valid JSON: {text[:300]}") from exc
