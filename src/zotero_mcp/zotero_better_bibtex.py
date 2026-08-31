"""Small Better BibTeX client for citation keys and BibTeX export."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from . import zotero_http, zotero_local

BETTER_BIBTEX_TRANSLATOR_ID = "ca65189f-8815-4afe-8c8b-8c7c15f0edca"


class BetterBibTeXError(RuntimeError):
    """Raised when the local Better BibTeX API cannot satisfy a request."""


@dataclass(frozen=True)
class BibTeXExport:
    item_key: str
    citation_key: str
    bibtex: str
    source: str = "better_bibtex"


def better_bibtex_url() -> str:
    parsed = urlsplit(zotero_local.api_base())
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return f"{origin}/better-bibtex/json-rpc"


def json_rpc(method: str, params: list[Any] | dict[str, Any]) -> Any:
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        response = zotero_http.post(
            better_bibtex_url(),
            route=zotero_http.RouteType.LOCAL,
            json=payload,
            timeout=30,
            headers={
                "Host": "localhost:23119",
                "Content-Type": "application/json",
                "Zotero-Allowed-Request": "1",
            },
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise BetterBibTeXError(
            "Better BibTeX is unavailable. Ensure Zotero is running and "
            "the Better BibTeX plugin is enabled."
        ) from exc

    error = data.get("error")
    if error:
        message = error.get("message", "Unknown Better BibTeX error")
        detail = error.get("data")
        if detail:
            message = f"{message}: {detail}"
        raise BetterBibTeXError(str(message))
    return data.get("result")


def citation_key(item_key: str) -> str:
    result = json_rpc("item.citationkey", {"item_keys": [item_key]})
    if not isinstance(result, dict) or not result.get(item_key):
        raise BetterBibTeXError(f"No Better BibTeX citation key found for {item_key}.")
    return str(result[item_key])


def export_bibtex(item_key: str) -> BibTeXExport:
    citekey = citation_key(item_key)
    result = json_rpc("item.export", [[citekey], BETTER_BIBTEX_TRANSLATOR_ID])
    bibtex = _export_text(result)
    if not bibtex.strip():
        raise BetterBibTeXError(
            f"Better BibTeX returned an empty export for {item_key}."
        )
    return BibTeXExport(item_key=item_key, citation_key=citekey, bibtex=bibtex)


def _export_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        for index in (2, 0):
            if len(result) > index and isinstance(result[index], str):
                return result[index]
    if isinstance(result, dict):
        for key in ("bibtex", "export", "text"):
            if isinstance(result.get(key), str):
                return result[key]
    raise BetterBibTeXError(
        f"Unexpected Better BibTeX export response: {type(result).__name__}."
    )


def citation_key_from_item(item: dict[str, Any]) -> str | None:
    data = item.get("data", {})
    native = data.get("citationKey")
    if native:
        return str(native).strip() or None
    for line in str(data.get("extra", "")).splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().casefold().replace(" ", "") == "citationkey":
            return value.strip() or None
    return None
