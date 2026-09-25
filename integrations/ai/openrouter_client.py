"""AI completion client — OpenRouter (verified against openrouter.ai/docs).

Every AI call in the project goes through here: OPS Manager Bot's routing and
answers, the answer checks, the Lead Agent. OpenRouter supports
``response_format: json_object``; ``complete_json`` also strips a fenced code
block, since some models wrap JSON in one regardless.

DeepSeek support was removed on 2026-09-25: the business moved to OpenRouter
+ Gemini on 2026-09-14 and removed the DeepSeek keys.
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

# Same-model retries specifically for an empty-content response, before
# falling through to the next model in the chain. See the comment at the
# call site in complete() for why this is scoped to that one failure mode.
EMPTY_CONTENT_RETRIES = 2

BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
EXTRA_HEADERS = {
    "HTTP-Referer": "https://mgmg-command-center.internal",
    "X-Title": "MGMG Command Center",
}
# Recorded as the target system on every audit row.
PROVIDER = "openrouter"


async def describe_openrouter_key() -> str:
    """One log-safe line saying which OpenRouter key is loaded and what it can spend.

    Shows only the key's last 4 characters. Exists so a credits/auth failure
    can be diagnosed from the service's own logs, instead of inferring which
    key a deployed service actually holds.
    """
    key = settings.openrouter_api_key.get_secret_value().strip()
    if not key:
        return "OpenRouter key: OPENROUTER_API_KEY is empty"
    suffix = f"...{key[-4:]}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), headers={"Authorization": f"Bearer {key}"}
        ) as client:
            credits = await client.get("https://openrouter.ai/api/v1/credits")
            key_info = await client.get("https://openrouter.ai/api/v1/key")
    except httpx.RequestError as exc:
        return f"OpenRouter key {suffix}: balance check errored: {exc}"

    if credits.status_code != 200:
        return f"OpenRouter key {suffix}: balance check failed HTTP {credits.status_code} {credits.text[:200]}"
    data = credits.json().get("data", {})
    loaded = float(data.get("total_credits") or 0)
    available = loaded - float(data.get("total_usage") or 0)
    key_data = key_info.json().get("data", {}) if key_info.status_code == 200 else {}
    return (
        f"OpenRouter key {suffix}: ${available:.2f} available of ${loaded:.2f} loaded, "
        f"per-key limit remaining={key_data.get('limit_remaining')}"
    )


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter returns an unrecoverable error."""


class OpenRouterClient:
    """Async client for OpenAI-compatible chat completions.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
        model_override: If set, used as the primary model instead of the
            global ``OPENROUTER_MODEL`` (OPS Manager Bot has its own).
        fallback_override: Comma-separated fallback chain to use alongside
            ``model_override``. Ignored if ``model_override`` is unset.
    """

    def __init__(
        self,
        agent: str = "-",
        run_id: uuid.UUID | str | None = None,
        *,
        model_override: str | None = None,
        fallback_override: str | None = None,
    ) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None
        self.provider = PROVIDER
        self._model_override = model_override
        self._fallback_override = fallback_override

    async def __aenter__(self) -> "OpenRouterClient":
        """Open the HTTP client with the OpenRouter key attached.

        Returns:
            The ready client.

        Raises:
            OpenRouterError: if the key is unset.
        """
        # strip(): a key pasted into a dashboard with a trailing newline/space
        # would otherwise become an invalid or rejected Authorization header.
        key = settings.openrouter_api_key.get_secret_value().strip()
        if not key:
            raise OpenRouterError("OpenRouter is not configured — fill OPENROUTER_API_KEY")

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **EXTRA_HEADERS,
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
            json_mode: Request a JSON-only response.

        Returns:
            The model's reply text.

        Raises:
            OpenRouterError: only if EVERY model in the chain failed.
        """
        chain = self.model_chain()
        last_error: Exception | None = None

        for index, model in enumerate(chain, start=1):
            # Empty-content responses (the model's turn ends with nothing in
            # `content`) showed up on ~46% of batches in production even with
            # reasoning disabled — a rate consistent with transient flakiness
            # rather than a deterministic per-model failure, so the same
            # model gets a couple of same-model retries before the run pays
            # for a fallback model. Other failure modes (HTTP errors, auth,
            # malformed JSON) are not retried here — those already had their
            # own retry pass in request_with_retry, or retrying won't help.
            for attempt in range(1, EMPTY_CONTENT_RETRIES + 2):
                try:
                    return await self._complete_with_model(model, system, user, json_mode)
                except OpenRouterError as exc:
                    last_error = exc
                    if "empty content" in str(exc) and attempt <= EMPTY_CONTENT_RETRIES:
                        log.warning(
                            "'{}' returned empty content (attempt {}/{}), retrying same model",
                            model,
                            attempt,
                            EMPTY_CONTENT_RETRIES + 1,
                        )
                        continue
                    break

            if index < len(chain):
                log.warning(
                    "Model '{}' failed ({}), falling back to '{}'",
                    model,
                    str(last_error)[:120],
                    chain[index],
                )

        raise OpenRouterError(
            f"All {len(chain)} model(s) failed ({', '.join(chain)}). Last error: {last_error}"
        )

    def model_chain(self) -> list[str]:
        """Return the ordered list of models to try.

        The primary model is always tried first, then any comma-separated
        entries in the matching fallback setting, deduplicated while
        preserving order.

        Returns:
            At least one model id.
        """
        if self._model_override:
            primary, fallbacks = self._model_override, (self._fallback_override or "")
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
            model: OpenRouter model id.
            system: System prompt.
            user: User message.
            json_mode: Request JSON-only output.

        Returns:
            The model's reply text.

        Raises:
            OpenRouterError: on any failure with this particular model.
        """
        assert self._client is not None
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
        }
        if json_mode:
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
                response = await request_with_retry(self._client, "POST", BASE_URL, json=payload)
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                ctx["http_status"] = getattr(getattr(exc, "response", None), "status_code", None)
                raise OpenRouterError(f"{self.provider} completion failed after retries: {exc}") from exc

            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                key_hint = self._client.headers.get("Authorization", "")[-4:]
                raise OpenRouterError(
                    f"{self.provider} completion failed (key ...{key_hint}): "
                    f"HTTP {response.status_code} {response.text[:300]}"
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
