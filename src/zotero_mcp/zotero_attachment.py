"""Generic Zotero imported-file attachment upload through Web API and WebDAV."""

from __future__ import annotations

import hashlib
import re
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from . import zotero_http, zotero_web_api

ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
WebDAVConfigLoader = Callable[
    [Mapping[str, str] | None], tuple[str, str, str, float, str]
]
AttachmentSelector = Callable[[dict[str, Any]], bool]


class ZoteroAttachmentError(RuntimeError):
    """Raised when an imported-file attachment cannot be written safely."""


def item_key(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    return str(data.get("key") or item.get("key") or "").upper()


def validate_pdf_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size < 5:
        raise ZoteroAttachmentError(f"invalid PDF output: {path}")
    with path.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise ZoteroAttachmentError(f"output is not a PDF: {path}")


def validate_filename(filename: str) -> str:
    value = str(filename).strip()
    if (
        not value
        or value in {".", ".."}
        or value.startswith(".")
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or not value.casefold().endswith(".pdf")
    ):
        raise ZoteroAttachmentError("filename must be one visible .pdf filename")
    return value


class ZoteroAttachmentClient:
    def __init__(
        self,
        *,
        config_loader: WebDAVConfigLoader,
        session: requests.Session | None = None,
        api: Any = zotero_web_api,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config_loader = config_loader
        self.session = zotero_http.routed_session(
            zotero_http.RouteType.NORMAL,
            session,
        )
        self.api = api
        self.environ = environ

    def configuration_status(self) -> dict[str, Any]:
        try:
            _, _, _, timeout, source = self.config_loader(self.environ)
        except Exception as exc:
            raise ZoteroAttachmentError(str(exc)) from exc
        return {"configured": True, "source": source, "timeout": timeout}

    def _config(self) -> tuple[str, str, str, float]:
        try:
            url, username, password, timeout, _ = self.config_loader(self.environ)
        except Exception as exc:
            raise ZoteroAttachmentError(str(exc)) from exc
        return url, username, password, timeout

    def _verify_webdav_write(self, base_url: str, timeout: float) -> None:
        probe_url = f"{base_url}zotero-mcp-write-test-{uuid.uuid4().hex}.tmp"
        put_error: ZoteroAttachmentError | None = None
        try:
            response = self.session.put(
                probe_url,
                data=b"",
                headers={"Content-Type": "application/octet-stream"},
                timeout=(10.0, min(timeout, 30.0)),
            )
            if response.status_code not in {200, 201, 204}:
                put_error = ZoteroAttachmentError(
                    f"WebDAV write check failed with HTTP {response.status_code}"
                )
        except requests.RequestException as exc:
            put_error = ZoteroAttachmentError(
                f"WebDAV write check failed: {type(exc).__name__}: {exc}"
            )

        try:
            cleanup = self.session.delete(
                probe_url,
                timeout=(10.0, min(timeout, 30.0)),
            )
        except requests.RequestException as exc:
            raise ZoteroAttachmentError(
                f"WebDAV write-check cleanup failed: {type(exc).__name__}: {exc}"
            ) from exc
        if cleanup.status_code not in {200, 204, 404}:
            raise ZoteroAttachmentError(
                f"WebDAV write-check cleanup failed with HTTP {cleanup.status_code}"
            )
        if put_error is not None:
            raise put_error

    def preflight(self, *, verify_write: bool = False) -> int:
        status = self.api.web_api_status()
        user_id = status.get("user_id")
        if not isinstance(user_id, int) or user_id < 1:
            raise ZoteroAttachmentError("Zotero Web API returned an invalid user_id")
        if status.get("files_write") is not True:
            raise ZoteroAttachmentError("Zotero Web API key lacks file write access")
        base_url, username, password, timeout = self._config()
        self.session.auth = (username, password)
        try:
            response = self.session.request(
                "PROPFIND",
                base_url,
                headers={"Depth": "0"},
                timeout=(10.0, min(timeout, 30.0)),
            )
        except requests.RequestException as exc:
            raise ZoteroAttachmentError(
                f"WebDAV credential check failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code not in {200, 204, 207}:
            raise ZoteroAttachmentError(
                f"WebDAV credential check failed with HTTP {response.status_code}"
            )
        if verify_write:
            self._verify_webdav_write(base_url, timeout)
        return user_id

    def _cloud_children(self, user_id: int, parent_key: str) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        start = 0
        while True:
            page = self.api.web_api_request_json(
                "GET",
                f"users/{user_id}/items/{parent_key}/children",
                params={"limit": 100, "start": start},
                timeout=30.0,
            )
            if not isinstance(page, list) or any(
                not isinstance(row, dict) for row in page
            ):
                raise ZoteroAttachmentError(
                    "Zotero Web API children response was invalid"
                )
            children.extend(page)
            if len(page) < 100:
                return children
            start += len(page)

    @staticmethod
    def _file_md5(path: Path) -> str:
        digest = hashlib.md5()  # Zotero WebDAV requires MD5.
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _created_attachment(response: Any) -> tuple[str, int | None]:
        if not isinstance(response, dict):
            raise ZoteroAttachmentError(
                "attachment creation returned invalid data; unknown write state"
            )
        entry: Any = (response.get("success") or {}).get("0")
        detailed = (response.get("successful") or {}).get("0")
        if entry is None:
            entry = detailed
        version: Any = (response.get("successVersions") or {}).get("0")
        if isinstance(detailed, dict):
            version = detailed.get("version", version)
        if isinstance(entry, dict):
            version = entry.get("version", version)
            entry = entry.get("key") or (entry.get("data") or {}).get("key")
        key = str(entry or "").upper()
        if not ZOTERO_KEY_RE.fullmatch(key):
            failure = (response.get("failed") or {}).get("0")
            if failure:
                raise ZoteroAttachmentError(
                    f"Zotero rejected attachment creation: {failure}"
                )
            raise ZoteroAttachmentError(
                "attachment creation returned no key; unknown write state"
            )
        return key, version if isinstance(version, int) and version > 0 else None

    @staticmethod
    def _validate_attachment(
        item: dict[str, Any],
        attachment_key: str,
        parent_key: str,
        filename: str,
        md5_hex: str,
        mtime_ms: int,
        expected_title: str,
    ) -> int:
        data = item.get("data") or {}
        checks = {
            "key": item_key(item) == attachment_key,
            "parentItem": str(data.get("parentItem") or "") == parent_key,
            "title": str(data.get("title") or "") == expected_title,
            "linkMode": str(data.get("linkMode") or "") == "imported_file",
            "contentType": str(data.get("contentType") or "") == "application/pdf",
            "filename": str(data.get("filename") or "") == filename,
            "md5": str(data.get("md5") or "").lower() == md5_hex,
        }
        try:
            checks["mtime"] = int(data.get("mtime")) == mtime_ms
        except (TypeError, ValueError):
            checks["mtime"] = False
        failed = [name for name, valid in checks.items() if not valid]
        if failed:
            raise ZoteroAttachmentError(
                f"Zotero attachment {attachment_key} failed read-back: "
                + ", ".join(failed)
            )
        version = item.get("version", data.get("version"))
        if not isinstance(version, int) or version < 1:
            raise ZoteroAttachmentError(
                f"Zotero attachment {attachment_key} has no version"
            )
        return version

    @staticmethod
    def _same_content(
        item: dict[str, Any], parent_key: str, filename: str, md5_hex: str
    ) -> bool:
        data = item.get("data") or {}
        return (
            str(data.get("parentItem") or "") == parent_key
            and str(data.get("linkMode") or "") == "imported_file"
            and str(data.get("filename") or "") == filename
            and str(data.get("md5") or "").lower() == md5_hex
        )

    def _upload_webdav(
        self,
        attachment_key: str,
        file_path: Path,
        filename: str,
        md5_hex: str,
        mtime_ms: int,
    ) -> None:
        base_url, username, password, timeout = self._config()
        self.session.auth = (username, password)
        encoded_key = quote(attachment_key, safe="")
        properties = (
            '<properties version="1">'
            f"<mtime>{mtime_ms}</mtime><hash>{md5_hex}</hash>"
            "</properties>"
        ).encode()
        with tempfile.TemporaryDirectory(prefix="zotero-webdav-") as temp_dir:
            archive = Path(temp_dir) / f"{attachment_key}.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.write(file_path, arcname=filename)
            with archive.open("rb") as handle:
                response = self.session.put(
                    f"{base_url}{encoded_key}.zip",
                    data=handle,
                    headers={"Content-Type": "application/zip"},
                    timeout=(10.0, timeout),
                )
                response.raise_for_status()
        response = self.session.put(
            f"{base_url}{encoded_key}.prop",
            data=properties,
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=(10.0, timeout),
        )
        response.raise_for_status()

    def _cleanup(
        self, user_id: int, attachment_key: str, version: int | None
    ) -> list[str]:
        errors: list[str] = []
        try:
            base_url, username, password, timeout = self._config()
            self.session.auth = (username, password)
            encoded_key = quote(attachment_key, safe="")
            for suffix in ("prop", "zip"):
                response = self.session.delete(
                    f"{base_url}{encoded_key}.{suffix}", timeout=(10.0, timeout)
                )
                if response.status_code not in {200, 204, 404}:
                    errors.append(f"WebDAV {suffix} DELETE HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
            errors.append(f"WebDAV cleanup failed: {exc}")
        try:
            if version is None:
                current = self.api.web_api_get_item(user_id, attachment_key)
                version = current.get(
                    "version", (current.get("data") or {}).get("version")
                )
            if not isinstance(version, int) or version < 1:
                raise ZoteroAttachmentError("attachment version is unavailable")
            response = self.api.web_api_request(
                "DELETE",
                f"users/{user_id}/items/{attachment_key}",
                headers={"If-Unmodified-Since-Version": str(version)},
                timeout=30.0,
            )
            if response.status_code not in {204, 404}:
                errors.append(f"Zotero attachment DELETE HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
            errors.append(f"Zotero attachment cleanup failed: {exc}")
        return errors

    def import_pdf(
        self,
        user_id: int,
        parent_key: str,
        file_path: Path,
        attachment_title: str,
        filename: str,
        *,
        existing_match: AttachmentSelector | None = None,
    ) -> dict[str, Any]:
        validate_pdf_file(file_path)
        filename = validate_filename(filename)
        attachment_title = str(attachment_title).strip()
        if not attachment_title:
            raise ZoteroAttachmentError("attachment title must not be empty")
        parent = self.api.web_api_get_item(user_id, parent_key)
        data = parent.get("data") or {}
        if data.get("itemType") in {"attachment", "annotation", "note"} or data.get(
            "parentItem"
        ):
            raise ZoteroAttachmentError(f"Zotero parent is not top-level: {parent_key}")
        md5_hex = self._file_md5(file_path)
        mtime_ms = int(file_path.stat().st_mtime * 1000)
        children = self._cloud_children(user_id, parent_key)
        if existing_match is None:
            existing_matches = [
                child
                for child in children
                if self._same_content(child, parent_key, filename, md5_hex)
            ]
        else:
            existing_matches = [child for child in children if existing_match(child)]
        if len(existing_matches) > 1:
            raise ZoteroAttachmentError(
                f"multiple matching attachments for {parent_key}"
            )
        existing = existing_matches[0] if existing_matches else None
        if existing:
            existing_key = item_key(existing)
            if not ZOTERO_KEY_RE.fullmatch(existing_key):
                raise ZoteroAttachmentError(
                    "existing attachment has no valid Zotero key"
                )
            refreshed = False
            if self._same_content(existing, parent_key, filename, md5_hex):
                existing_data = existing.get("data") or {}
                try:
                    upload_mtime = int(existing_data.get("mtime"))
                except (TypeError, ValueError):
                    upload_mtime = mtime_ms
                self._upload_webdav(
                    existing_key, file_path, filename, md5_hex, upload_mtime
                )
                refreshed = True
            return {
                "ok": True,
                "already_present": True,
                "attachment_key": existing_key,
                "webdav_refreshed": refreshed,
            }

        payload = {
            "itemType": "attachment",
            "linkMode": "imported_file",
            "title": attachment_title,
            "parentItem": parent_key,
            "contentType": "application/pdf",
            "charset": "",
            "filename": filename,
            "md5": md5_hex,
            "mtime": mtime_ms,
            "note": "",
            "tags": [],
            "relations": {},
        }
        attachment_key = ""
        version: int | None = None
        try:
            creation = self.api.web_api_request_json(
                "POST",
                f"users/{user_id}/items",
                payload=[payload],
                headers={"Zotero-Write-Token": uuid.uuid4().hex},
                timeout=45.0,
            )
            attachment_key, version = self._created_attachment(creation)
            created = self.api.web_api_get_item(user_id, attachment_key)
            version = self._validate_attachment(
                created,
                attachment_key,
                parent_key,
                filename,
                md5_hex,
                mtime_ms,
                attachment_title,
            )
            self._upload_webdav(attachment_key, file_path, filename, md5_hex, mtime_ms)
            verified = self.api.web_api_get_item(user_id, attachment_key)
            self._validate_attachment(
                verified,
                attachment_key,
                parent_key,
                filename,
                md5_hex,
                mtime_ms,
                attachment_title,
            )
        except Exception as exc:
            if attachment_key:
                cleanup_errors = self._cleanup(user_id, attachment_key, version)
                if cleanup_errors:
                    raise ZoteroAttachmentError(
                        f"attachment import failed for {attachment_key}: {exc}; "
                        "cleanup incomplete: " + "; ".join(cleanup_errors)
                    ) from exc
            if isinstance(exc, ZoteroAttachmentError):
                raise
            raise ZoteroAttachmentError(
                f"Zotero attachment import failed: {exc}"
            ) from exc
        return {
            "ok": True,
            "already_present": False,
            "attachment_key": attachment_key,
            "title": attachment_title,
            "filename": filename,
        }
