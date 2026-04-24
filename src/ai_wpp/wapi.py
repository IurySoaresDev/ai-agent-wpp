"""Thin async HTTP client for the W-API REST API.

Only the surfaces the agent needs today are implemented (send text + a typing
indicator helper). Everything else can be added as new typed methods.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)


class WApiError(RuntimeError):
    """Raised when the W-API returns a non-2xx response we cannot recover from."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"W-API error {status}: {body[:400]}")
        self.status = status
        self.body = body


# Transient failures we want to retry automatically.
_RETRYABLE = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
)


class WApiClient:
    """Async client bound to a single W-API instance."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.wapi_base_url,
            headers={
                "Authorization": f"Bearer {settings.wapi_token.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=5.0),
        )

    # Context manager -------------------------------------------------------
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # Public API ------------------------------------------------------------
    async def send_text(
        self,
        *,
        phone: str,
        message: str,
        reply_to: str | None = None,
        delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Send a plain text WhatsApp message.

        Returns the W-API response body decoded as JSON.
        """
        payload: dict[str, Any] = {"phone": phone, "message": message}
        if reply_to:
            payload["messageId"] = reply_to
        if delay_seconds is not None:
            payload["delayMessage"] = max(1, min(15, int(delay_seconds)))

        return await self._post_json(
            path="/message/send-text",
            json=payload,
        )

    # Internal --------------------------------------------------------------
    async def _post_json(self, *, path: str, json: dict[str, Any]) -> dict[str, Any]:
        params = {"instanceId": self._settings.wapi_instance_id}

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                response = await self._client.post(path, params=params, json=json)

        if response.status_code >= 500:
            # Server-side — raise so the caller can decide how to handle it.
            log.warning(
                "wapi.server_error",
                status=response.status_code,
                path=path,
                body=response.text[:500],
            )
            raise WApiError(response.status_code, response.text)

        if response.status_code >= 400:
            log.error(
                "wapi.client_error",
                status=response.status_code,
                path=path,
                body=response.text[:500],
            )
            raise WApiError(response.status_code, response.text)

        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError:
            return {"raw": response.text}
