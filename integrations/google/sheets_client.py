"""Google Sheets client using a service account (no interactive OAuth).

Verified against Google's own REST reference on 2026-08-19
(developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values).

Auth: signs a JWT with the service account's RS256 private key and exchanges
it for an access token via the standard OAuth2 service-account flow
(RFC 7523), then uses that token as a Bearer header — the same shape as every
other client in this project (SAP session, Graph app-only token), just
implemented directly with ``pyjwt`` instead of pulling in the full
``google-api-python-client``/``google-auth`` stack for what is really two
REST calls.

The private key never appears in a log line or an audit row — only the
resulting short-lived access token does, and even that is not logged.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import jwt

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("sheets")

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class SheetsError(RuntimeError):
    """Raised when the Sheets API returns an error the client cannot recover from."""


class SheetsClient:
    """Async client for one Google Sheets spreadsheet.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
        spreadsheet_id: Target spreadsheet; defaults to ``GOOGLE_LEADS_SHEET_ID``.
    """

    def __init__(
        self,
        agent: str = "-",
        run_id: uuid.UUID | str | None = None,
        spreadsheet_id: str | None = None,
    ) -> None:
        self.agent = agent
        self.run_id = run_id
        self.spreadsheet_id = spreadsheet_id or settings.google_leads_sheet_id
        self._client: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    async def __aenter__(self) -> "SheetsClient":
        """Open the HTTP client and acquire the first access token.

        Returns:
            The ready client.

        Raises:
            SheetsError: if the service account JSON or sheet id is unset,
                or if the JSON is malformed.
        """
        if not settings.google_service_account_json.get_secret_value():
            raise SheetsError("Google Sheets is not configured — fill GOOGLE_SERVICE_ACCOUNT_JSON in .env")
        if not self.spreadsheet_id:
            raise SheetsError("No spreadsheet id — fill GOOGLE_LEADS_SHEET_ID in .env or pass spreadsheet_id")

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        await self._authenticate()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- auth

    async def _authenticate(self) -> None:
        """Sign a service-account JWT and exchange it for an access token.

        Raises:
            SheetsError: if the service account JSON is malformed or Google
                rejects the token request.
        """
        try:
            creds = json.loads(settings.google_service_account_json.get_secret_value())
        except json.JSONDecodeError as exc:
            raise SheetsError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc

        now = int(time.time())
        claims = {
            "iss": creds["client_email"],
            "scope": SCOPE,
            "aud": creds.get("token_uri", TOKEN_URL),
            "iat": now,
            "exp": now + 3600,
        }
        assertion = jwt.encode(claims, creds["private_key"], algorithm="RS256")

        assert self._client is not None
        async with audited(
            agent=self.agent,
            action="authenticate",
            target_system="google_sheets",
            run_id=self.run_id,
            target_ref=creds.get("client_email"),
        ) as ctx:
            response = await request_with_retry(
                self._client,
                "POST",
                creds.get("token_uri", TOKEN_URL),
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SheetsError(f"Google token exchange failed: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()

        self._access_token = body["access_token"]
        self._token_expires_at = time.time() + int(body.get("expires_in", 3600)) - 60
        log.info("Google Sheets token acquired for {}", creds.get("client_email"))

    async def _ensure_token(self) -> None:
        """Refresh the access token if it is missing or about to expire."""
        if self._access_token is None or time.time() >= self._token_expires_at:
            await self._authenticate()

    # ------------------------------------------------------------- requests

    async def get_values(self, a1_range: str) -> list[list[str]]:
        """Read a range of cell values.

        Args:
            a1_range: A1 notation, e.g. ``'Leads!A:F'`` or ``'Leads!A1:A1'``.

        Returns:
            Rows of string cell values (Sheets omits trailing empty cells per
            row, so rows may have different lengths). Empty list if the range
            has no data.

        Raises:
            SheetsError: on a non-200 response.
        """
        await self._ensure_token()
        assert self._client is not None
        url = f"{SHEETS_API_ROOT}/{self.spreadsheet_id}/values/{a1_range}"

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="google_sheets",
            run_id=self.run_id,
            target_ref=a1_range,
            mode="read",
        ) as ctx:
            response = await request_with_retry(
                self._client, "GET", url, headers={"Authorization": f"Bearer {self._access_token}"}
            )
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SheetsError(f"Sheets read failed: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()
            values = body.get("values", [])
            ctx["payload"]["rows"] = len(values)

        log.info("Sheets: read {} row(s) from {}", len(values), a1_range)
        return values

    async def append_rows(self, a1_range: str, rows: list[list[str]]) -> int:
        """Append rows to a sheet, after the last row with data.

        Args:
            a1_range: A1 notation naming the sheet/table to append to, e.g.
                ``'Leads!A:F'``.
            rows: Row values to append, outermost list = rows.

        Returns:
            Number of rows appended (0 if ``rows`` is empty — no request sent).

        Raises:
            SheetsError: on a non-200 response.
        """
        if not rows:
            return 0

        await self._ensure_token()
        assert self._client is not None
        url = f"{SHEETS_API_ROOT}/{self.spreadsheet_id}/values/{a1_range}:append"

        async with audited(
            agent=self.agent,
            action="append_rows",
            target_system="google_sheets",
            run_id=self.run_id,
            target_ref=a1_range,
            mode="write",
            payload={"row_count": len(rows)},
        ) as ctx:
            response = await request_with_retry(
                self._client,
                "POST",
                url,
                params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                headers={"Authorization": f"Bearer {self._access_token}"},
                json={"values": rows},
            )
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SheetsError(f"Sheets append failed: HTTP {response.status_code} {response.text[:300]}")

        log.info("Sheets: appended {} row(s) to {}", len(rows), a1_range)
        return len(rows)

    async def get_sheet_id(self, sheet_name: str) -> int:
        """Look up a tab's numeric ``sheetId`` (gid), required for row deletion.

        Args:
            sheet_name: The tab name as shown in the spreadsheet UI, e.g. ``'Sheet1'``.

        Returns:
            The tab's numeric sheetId.

        Raises:
            SheetsError: on a non-200 response, or if no tab has that name.
        """
        await self._ensure_token()
        assert self._client is not None
        url = f"{SHEETS_API_ROOT}/{self.spreadsheet_id}"

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="google_sheets",
            run_id=self.run_id,
            target_ref="spreadsheets.get",
            mode="read",
        ) as ctx:
            response = await request_with_retry(
                self._client,
                "GET",
                url,
                params={"fields": "sheets.properties"},
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SheetsError(f"Sheets metadata read failed: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()

        for sheet in body.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == sheet_name:
                return props["sheetId"]
        raise SheetsError(f"No tab named '{sheet_name}' in spreadsheet {self.spreadsheet_id}")

    async def delete_rows(self, sheet_id: int, row_numbers: list[int]) -> int:
        """Delete specific rows (1-indexed, as shown in the sheet UI) via batchUpdate.

        Args:
            sheet_id: Numeric tab id from :meth:`get_sheet_id`.
            row_numbers: 1-indexed row numbers to delete, e.g. row 5 = the 5th
                row including the header. Order does not matter — deletion is
                always applied highest-row-first internally so earlier deletes
                cannot shift the indices of later ones.

        Returns:
            Number of rows deleted.

        Raises:
            SheetsError: on a non-200 response.
        """
        if not row_numbers:
            return 0

        await self._ensure_token()
        assert self._client is not None
        url = f"{SHEETS_API_ROOT}/{self.spreadsheet_id}:batchUpdate"

        # Descending order: deleting row 10 then row 5 is safe, but deleting
        # row 5 then row 10 would delete the wrong row the second time since
        # everything below row 5 has already shifted up by one.
        requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row - 1,  # API is 0-indexed
                        "endIndex": row,
                    }
                }
            }
            for row in sorted(set(row_numbers), reverse=True)
        ]

        async with audited(
            agent=self.agent,
            action="delete_rows",
            target_system="google_sheets",
            run_id=self.run_id,
            target_ref=f"sheetId={sheet_id}",
            mode="write",
            payload={"row_count": len(requests), "rows": sorted(set(row_numbers))},
        ) as ctx:
            response = await request_with_retry(
                self._client,
                "POST",
                url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                json={"requests": requests},
            )
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SheetsError(f"Sheets row delete failed: HTTP {response.status_code} {response.text[:300]}")

        log.info("Sheets: deleted {} row(s) from sheetId {}", len(requests), sheet_id)
        return len(requests)
