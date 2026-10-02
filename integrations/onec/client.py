"""1C over the standard OData interface — read-only.

Our 1C is «Бухгалтерия для Узбекистана» on Clobus.uz (1C:Fresh), database
«EMJIEM Бухгалтерия» — address
``https://clobus.uz/a/acc313/61458/odata/standard.odata/`` (set as
ONEC_ODATA_URL). Access, as 1C:Fresh documents it
(https://1cfresh.com/articles/data_odata): a service user made in
Администрирование → Синхронизация данных → Настройки стандартного
интерфейса OData (role УдаленныйДоступOData), HTTP Basic auth, and only the
objects ticked on its «Состав» tab are visible.

Read-only by construction: this client has a GET method and nothing else —
no POST/PATCH/DELETE can be sent through it, whatever 1C would allow.

Entity names are Cyrillic (``ChartOfAccounts_Хозрасчетный``); they are
percent-encoded in the path. ``$format=json`` gives ``{"value": [...]}``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("onec")


class OneCError(RuntimeError):
    """1C could not be read (not configured, refused, or answered nonsense)."""


class OneCClient:
    """GET-only OData client.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
        transport: Test hook — an ``httpx`` transport instead of the network.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OneCClient":
        if not settings.onec_configured:
            raise OneCError("1C is not configured (ONEC_ODATA_URL / ONEC_LOGIN / ONEC_PASSWORD)")
        base = settings.onec_odata_url.strip().rstrip("/") + "/"
        self._http = httpx.AsyncClient(
            base_url=base,
            auth=(settings.onec_login.strip(), settings.onec_password.get_secret_value()),
            timeout=90.0,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def get(self, resource: str, params: dict[str, str] | None = None) -> Any:
        """GET one OData resource (e.g. ``ChartOfAccounts_Хозрасчетный``) as JSON."""
        assert self._http is not None
        path = quote(resource, safe="/()=',$:")
        async with audited(agent=self.agent, action="odata_get", target_system="1c", run_id=self.run_id,
                           target_ref=resource[:200]):
            response = await request_with_retry(
                self._http, "GET", path, params={"$format": "json", **(params or {})}
            )
        if response.status_code >= 400:
            raise OneCError(f"{resource[:80]}: HTTP {response.status_code} — {_odata_message(response.text)}")
        try:
            return json.loads(response.text)
        except ValueError as exc:
            raise OneCError(f"{resource[:80]}: not JSON — {response.text[:200]}") from exc

    async def entity_sets(self) -> list[str]:
        """What the service user can see (the service document)."""
        body = await self.get("")
        return [str(item.get("name") or item.get("url")) for item in (body or {}).get("value", [])]


def _odata_message(text: str) -> str:
    """1C's own error text, without the noise."""
    try:
        body = json.loads(text)
        message = ((body.get("odata.error") or {}).get("message") or {}).get("value")
        if message:
            return str(message)[:300]
    except (ValueError, AttributeError):
        pass
    return text[:300]
