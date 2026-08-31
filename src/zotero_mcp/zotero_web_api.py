#!/usr/bin/env python3
"""Zotero Web API credentials, HTTP transport, and read primitives."""

from __future__ import annotations

import os
import stat
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from . import zotero_http, zotero_runtime

DEFAULT_WEB_API_BASE = "https://api.zotero.org"
DEFAULT_WEB_API_KEY_FILE = "zotero_web_api_key.secret"
WEB_API_VERSION = "3"
USER_AGENT = "zotero-mcp-local"
READ_RETRY_STATUSES = {429, 503}
DEFAULT_RATE_LIMIT_DELAY = 1.0
_backoff_lock = threading.Lock()
_backoff_until = 0.0


class ZoteroWriteError(RuntimeError):
    """Expected workflow or Web API error with a user-actionable message."""


class ZoteroNotSyncedError(ZoteroWriteError):
    """A local Zotero item has not reached the Zotero cloud library."""


class ZoteroCloudConflictError(ZoteroWriteError):
    """Exact identifiers resolve to multiple cloud items."""

    def __init__(self, message: str, item_keys: list[str]) -> None:
        super().__init__(message)
        self.item_keys = item_keys


class ZoteroVersionConflictError(ZoteroWriteError):
    """A versioned write was rejected before the requested deletion occurred."""


def _header_delay(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _response_delay(response: requests.Response) -> float | None:
    delays = [
        delay
        for delay in (
            _header_delay(response.headers.get("Backoff")),
            _header_delay(response.headers.get("Retry-After")),
        )
        if delay is not None
    ]
    return max(delays) if delays else None


def _record_backoff(delay: float | None) -> None:
    if delay is None or delay <= 0:
        return
    global _backoff_until
    with _backoff_lock:
        _backoff_until = max(_backoff_until, time.monotonic() + delay)


def _wait_for_backoff() -> None:
    with _backoff_lock:
        delay = _backoff_until - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def web_api_base_url() -> str:
    return (
        os.environ.get("ZOTERO_WEB_API_URL", DEFAULT_WEB_API_BASE).strip().rstrip("/")
    )


def expected_user_id() -> int | None:
    raw = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not raw:
        return zotero_runtime.config_positive_int("zotero", "user_id")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ZoteroWriteError("ZOTERO_USER_ID must be a positive integer") from exc
    if value < 1:
        raise ZoteroWriteError("ZOTERO_USER_ID must be a positive integer")
    return value


def web_api_key() -> str:
    value = os.environ.get("ZOTERO_API_KEY", "").strip()
    if value:
        return value

    configured = zotero_runtime.configured_path(
        "ZOTERO_API_KEY_FILE", "zotero", "api_key_file"
    )
    key_file = configured or zotero_runtime.default_secret_path(
        DEFAULT_WEB_API_KEY_FILE
    )
    if not key_file.is_file():
        raise ZoteroWriteError(
            f"Zotero Web API key not found. Set ZOTERO_API_KEY or create {key_file}."
        )
    mode = stat.S_IMODE(key_file.stat().st_mode)
    if os.name != "nt" and mode & 0o077:
        raise ZoteroWriteError(
            f"Zotero Web API key file must use permission 600: {key_file}"
        )
    value = key_file.read_text(encoding="utf-8").strip()
    if not value:
        raise ZoteroWriteError(f"Zotero Web API key file is empty: {key_file}")
    return value


def web_api_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> requests.Response:
    url = f"{web_api_base_url()}/{path.lstrip('/')}"
    request_headers = {
        "Zotero-API-Key": web_api_key(),
        "Zotero-API-Version": WEB_API_VERSION,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    method_upper = method.upper()
    max_attempts = 2 if method_upper in {"GET", "HEAD"} else 1
    for attempt in range(max_attempts):
        _wait_for_backoff()
        try:
            request_kwargs = {
                "params": params,
                "json": payload,
                "headers": request_headers,
                "timeout": timeout,
            }
            response = zotero_http.request(
                method,
                url,
                route=zotero_http.RouteType.NORMAL,
                **request_kwargs,
            )
        except requests.RequestException as exc:
            if method_upper in {"GET", "HEAD"} and attempt + 1 < max_attempts:
                continue
            if method_upper in {"POST", "PATCH", "PUT", "DELETE"}:
                message = "Zotero Web API request failed with unknown write state"
            else:
                message = "Zotero Web API read failed"
            raise ZoteroWriteError(
                f"{message}: {type(exc).__name__}: {exc}. Rescan before retrying."
            ) from exc
        delay = _response_delay(response)
        if response.status_code in READ_RETRY_STATUSES and delay is None:
            delay = DEFAULT_RATE_LIMIT_DELAY
        _record_backoff(delay)
        if (
            method_upper in {"GET", "HEAD"}
            and response.status_code in READ_RETRY_STATUSES
            and attempt + 1 < max_attempts
        ):
            continue
        return response
    raise AssertionError("unreachable")


def web_api_error(response: requests.Response) -> ZoteroWriteError:
    message = response.text.strip()[:500]
    try:
        data = response.json()
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("error") or message)
    except ValueError:
        pass
    suffix = f": {message}" if message else ""
    return ZoteroWriteError(f"Zotero Web API HTTP {response.status_code}{suffix}")


def web_api_request_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Any:
    response = web_api_request(
        method,
        path,
        params=params,
        payload=payload,
        headers=headers,
        timeout=timeout,
    )
    if not response.ok:
        raise web_api_error(response)
    try:
        return response.json()
    except ValueError as exc:
        state = "unknown write state" if method.upper() != "GET" else "invalid response"
        raise ZoteroWriteError(
            f"Zotero Web API returned HTTP {response.status_code} with non-JSON content; {state}."
        ) from exc


def _personal_library_write_allowed(access: dict[str, Any]) -> bool:
    user_access = access.get("user") or {}
    library = user_access.get("library")
    return (
        library is True
        or str(library).lower() == "write"
        or user_access.get("write") is True
    )


def web_api_status() -> dict[str, Any]:
    data = web_api_request_json("GET", "keys/current", timeout=15.0)
    if not isinstance(data, dict):
        raise ZoteroWriteError("Zotero Web API key status returned invalid JSON")
    try:
        user_id = int(data.get("userID"))
    except (TypeError, ValueError) as exc:
        raise ZoteroWriteError(
            "Zotero Web API key status returned no valid userID"
        ) from exc
    expected = expected_user_id()
    if expected is not None and user_id != expected:
        raise ZoteroWriteError(
            f"Zotero Web API key belongs to user {user_id}, expected {expected}"
        )
    access = data.get("access") or {}
    if not _personal_library_write_allowed(access):
        raise ZoteroWriteError(
            "Zotero Web API key lacks write access to the personal library"
        )
    user_access = access.get("user") or {}
    return {
        "ok": True,
        "transport": "zotero_web_api",
        "api_base": web_api_base_url(),
        "user_id": user_id,
        "personal_library_write": True,
        "files_write": bool(user_access.get("files")),
    }


def web_api_get_item(user_id: int, item_key: str) -> dict[str, Any]:
    response = web_api_request("GET", f"users/{user_id}/items/{item_key}", timeout=20.0)
    if response.status_code == 404:
        raise ZoteroNotSyncedError(
            f"local item {item_key} is not synced to Zotero Web API"
        )
    if not response.ok:
        raise web_api_error(response)
    try:
        item = response.json()
    except ValueError as exc:
        raise ZoteroWriteError(
            "Zotero Web API item response was not valid JSON"
        ) from exc
    if not isinstance(item, dict):
        raise ZoteroWriteError("Zotero Web API item response was not an object")
    return item


def web_api_search_items(user_id: int, query: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start = 0
    while True:
        response = web_api_request(
            "GET",
            f"users/{user_id}/items/top",
            params={
                "q": query,
                "qmode": "everything",
                "limit": 100,
                "start": start,
            },
            timeout=30.0,
        )
        if not response.ok:
            raise web_api_error(response)
        try:
            page = response.json()
        except ValueError as exc:
            raise ZoteroWriteError(
                "Zotero Web API exact-identifier search returned invalid JSON"
            ) from exc
        if not isinstance(page, list):
            raise ZoteroWriteError(
                "Zotero Web API exact-identifier search returned invalid data"
            )
        if any(not isinstance(item, dict) for item in page):
            raise ZoteroWriteError(
                "Zotero Web API exact-identifier search returned a non-object item"
            )
        items.extend(page)
        total_raw = response.headers.get("Total-Results")
        if total_raw is None:
            if not page or len(page) < 100:
                break
            raise ZoteroWriteError(
                "Zotero Web API exact-identifier search omitted Total-Results for a full page"
            )
        try:
            total = int(total_raw)
        except (TypeError, ValueError) as exc:
            raise ZoteroWriteError(
                "Zotero Web API exact-identifier search returned invalid Total-Results"
            ) from exc
        if not page or len(items) >= total or len(page) < 100:
            break
        start += len(page)
    return items
