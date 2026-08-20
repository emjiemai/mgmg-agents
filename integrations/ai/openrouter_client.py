"""AI completion client — used by the Lead Agent to qualify raw search hits.

Supports two providers behind one interface, selected by ``AI_PROVIDER``:

- **openrouter** — verified against openrouter.ai/docs/quickstart on
  2026-08-19. Confirmed to support ``response_format: json_object``.
- **deepseek** — DeepSeek's own API, verified against api-docs.deepseek.com
  on 2026-08-19. Also OpenAI-compatible (``chat/completions``, Bearer auth,
  reply at ``choices[0].message.content``), but its docs do not confirm
  ``response_format`` support, so JSON mode is only requested for OpenRouter;
  DeepSeek relies on the prompt's own JSON instruction plus the defensive
  fenced-code-block stripping already in ``complete_json``.

Switching providers is a one-line env change (``AI_PROVIDER=deepseek`` or
``AI_PROVIDER=openrouter``), not a code change — this class stays named
``OpenRouterClient`` for import stability even though it now serves both,
since the class itself is generic (base URL, auth, and JSON-mode support are
the only per-provider differences).
"""

from __future__ import annotations

import json
import uuid

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("ai")

# A batch of 60 leads' worth of structured JSON comfortably fits in well
# under this; the cap exists to stop the provider defaulting to a model's
# full max output (which a low-credit account can be refused outright for —
# see the comment at the call site).
DEFAULT_MAX_TOKENS = 8000

PROVIDERS: dict[str, dict[str, object]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "supports_json_mode": True,
        "extra_headers": {
            "HTTP-Referer": "https://mgmg-command-center.internal",
            "X-Title": "MGMG Lead Agent",
        },
        "extra_payload": {},
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/chat/completions",
        "supports_json_mode": False,
        "extra_headers": {},
        # DeepSeek's reasoning-capable models emit chain-of-thought into a
        # SEPARATE `reasoning_content` field before the final answer in
        # `content` -- confirmed 2026-08-20 after batches failed with an
        # empty `content` (the model spent its turn reasoning without ever
        # committing to a final answer). This is a plain classification
        # task with no need for that, so thinking is turned off: faster,
        # cheaper, and removes the empty-content failure mode entirely.
        "extra_payload": {"thinking": {"type": "disabled"}},
    },
}


class OpenRouterError(RuntimeError):
    """Raised when the configured AI provider returns an unrecoverable error."""


class OpenRouterClient:
    """Async client for OpenAI-compatible chat completions.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None
        self.provider = settings.ai_provider.strip().lower()

    async def __aenter__(self) -> "OpenRouterClient":
        """Open the HTTP client with the configured provider's auth attached.

        Returns:
            The ready client.

        Raises:
            OpenRouterError: if the provider is unknown or its key is unset.
        """
        if self.provider not in PROVIDERS:
            raise OpenRouterError(
                f"Unknown AI_PROVIDER '{self.provider}' — use 'openrouter' or 'deepseek'"
            )

        key = (
            settings.deepseek_api_key.get_secret_value()
            if self.provider == "deepseek"
            else settings.openrouter_api_key.get_secret_value()
        )
        if not key:
            raise OpenRouterError(
                f"{self.provider} is not configured — fill "
                f"{'DEEPSEEK_API_KEY' if self.provider == 'deepseek' else 'OPENROUTER_API_KEY'} in .env"
            )

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **PROVIDERS[self.provider]["extra_headers"],
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

        Tries each model in the fallback chain in turn (see ``model_chain``),
        so a single congested or rate-limited model cannot fail the run.

        Args:
            system: System prompt.
            user: User message.
            json_mode: Request a JSON-only response. Only sent when the
                active provider is confirmed to support it (OpenRouter); on
                DeepSeek this is a no-op and callers should rely on prompt
                instructions plus defensive parsing instead.

        Returns:
            The model's reply text.

        Raises:
            OpenRouterError: only if EVERY model in the chain failed.
        """
        chain = self.model_chain()
        last_error: Exception | None = None

        for index, model in enumerate(chain, start=1):
            try:
                return await self._complete_with_model(model, system, user, json_mode)
            except OpenRouterError as exc:
                last_error = exc
                if index < len(chain):
                    log.warning(
                        "Model '{}' failed ({}), falling back to '{}'",
                        model,
                        str(exc)[:120],
                        chain[index],
                    )

        raise OpenRouterError(
            f"All {len(chain)} model(s) failed ({', '.join(chain)}). Last error: {last_error}"
        )

    def model_chain(self) -> list[str]:
        """Return the ordered list of models to try for the active provider.

        The primary model is always tried first, then any comma-separated
        entries in the matching fallback setting, deduplicated while
        preserving order.

        Returns:
            At least one model id.
        """
        if self.provider == "deepseek":
            primary, fallbacks = settings.deepseek_model, settings.deepseek_fallback_models
        else:
            primary, fallbacks = settings.openrouter_model, settings.openrouter_fallback_models

        chain = [primary.strip()]
        for fallback in fallbacks.split(","):
            fallback = fallback.strip()
            if fallback and fallback not in chain:
                chain.append(fallback)
        return [m for m in chain if m]

    async def _complete_with_model(
        self, model: str, system: str, user: str, json_mode: bool
    ) -> str:
        """Run one completion against one specific model.

        Args:
            model: Model id for the active provider.
            system: System prompt.
            user: User message.
            json_mode: Request JSON-only output, if the provider supports it.

        Returns:
            The model's reply text.

        Raises:
            OpenRouterError: on any failure with this particular model.
        """
        assert self._client is not None
        base_url = str(PROVIDERS[self.provider]["base_url"])
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Without an explicit cap, some providers default to the model's
            # own max output (65,536 for some models) and then check that
            # against account balance -- a low-credit account gets a hard 402
            # ("requested up to 65536 tokens, but can only afford X") even
            # though the actual reply needed is a few thousand tokens. Capping
            # here avoids that regardless of account balance.
            "max_tokens": DEFAULT_MAX_TOKENS,
            **PROVIDERS[self.provider]["extra_payload"],
        }
        if json_mode and PROVIDERS[self.provider]["supports_json_mode"]:
            payload["response_format"] = {"type": "json_object"}

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system=self.provider,
            run_id=self.run_id,
            target_ref=model,
            payload={"prompt_chars": len(system) + len(user), "model": model},
        ) as ctx:
            # request_with_retry raises the raw httpx exception once retries
            # are exhausted (e.g. a 429 that never clears) rather than
            # returning a response — that must still surface as OpenRouterError
            # so callers catching our typed error (not just Exception) degrade
            # gracefully instead of crashing the whole agent run.
            try:
                response = await request_with_retry(self._client, "POST", base_url, json=payload)
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                ctx["http_status"] = getattr(getattr(exc, "response", None), "status_code", None)
                raise OpenRouterError(f"{self.provider} completion failed after retries: {exc}") from exc

            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise OpenRouterError(
                    f"{self.provider} completion failed: HTTP {response.status_code} {response.text[:300]}"
                )
            body = response.json()
            choices = body.get("choices", [])
            if not choices:
                raise OpenRouterError(f"{self.provider} returned no choices: {body}")

            message = choices[0].get("message", {})
            text = message.get("content") or ""
            if not text.strip() and message.get("reasoning_content"):
                # Rescue path: a reasoning-capable model occasionally puts its
                # actual answer at the tail of the chain-of-thought instead of
                # committing it to `content` — try to salvage it rather than
                # failing outright when `content` came back empty.
                log.warning(
                    "{}: empty content, falling back to reasoning_content ({} chars)",
                    self.provider,
                    len(message["reasoning_content"]),
                )
                text = message["reasoning_content"]

            if not text.strip():
                raise OpenRouterError(
                    f"{self.provider} returned empty content. finish_reason="
                    f"{choices[0].get('finish_reason')}, raw={str(body)[:400]}"
                )
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
                first, since some models wrap JSON in ```json regardless of
                whether JSON mode was requested or supported).
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
            raise OpenRouterError(f"{self.provider} reply was not valid JSON: {text[:300]}") from exc
