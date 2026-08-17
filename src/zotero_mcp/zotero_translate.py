#!/usr/bin/env python3
"""Queue and run unattended Zotero PDF translations."""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from . import zotero_local, zotero_runtime, zotero_web_api
from .zotero_collections import resolve_collection

QUEUE_FIELDS = (
    "paper_title",
    "parent_item_key",
    "source_attachment_key",
    "status",
    "output_pdf",
    "last_error",
)
VALID_STATUSES = {"pending", "translating", "importing", "done", "failed"}
DEFAULT_TRANSLATION_ATTACHMENT_TITLE = "CN"
DEFAULT_TRANSLATION_FILENAME_TEMPLATE = "{source_stem}的全文翻译.pdf"
CN_TITLE = DEFAULT_TRANSLATION_ATTACHMENT_TITLE
PDF2ZH_TRANSLATION_FILENAME_RE = re.compile(
    r"\.zh(?:[-_]?cn)?\.(?:mono|dual)\.pdf$", re.IGNORECASE
)
PREF_PREFIX = "extensions.zotero.pdf2zh."
PREF_RE = re.compile(r'^user_pref\("(?P<name>[^"]+)",\s*(?P<value>.+)\);$')
ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
FREE_SERVICES = {"bing", "google", "siliconflowfree"}
MAX_ERROR_LENGTH = 1000
DEFAULT_WEBDAV_TIMEOUT = 300.0
WEBDAV_ENV_NAMES = (
    "ZOTERO_WEBDAV_URL",
    "ZOTERO_WEBDAV_USERNAME",
    "ZOTERO_WEBDAV_PASSWORD",
)
ALLOW_INSECURE_HTTP_ENV = "ZOTERO_TRANSLATE_ALLOW_INSECURE_HTTP"
RENAME_WATCH_SERVICE_NAME = "zotero-translate-rename-watch"
TRUE_VALUES = {"1", "true", "yes", "on"}
SENSITIVE_KEY_RE = re.compile(
    r"(?:api[-_]?key|token|secret|password|authorization|credential)", re.IGNORECASE
)
REQUEST_PREFS = (
    "sourceLang",
    "targetLang",
    "skipLastPages",
    "threadNum",
    "fontFamily",
    "dualMode",
    "transFirst",
    "ocr",
    "autoOcr",
    "saveGlossary",
    "disableGlossary",
    "skipClean",
    "disableRichTextTranslate",
    "enhanceCompatibility",
    "translateTableText",
    "onlyIncludeTranslatedPage",
)


class TranslationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranslationNaming:
    attachment_title: str = DEFAULT_TRANSLATION_ATTACHMENT_TITLE
    filename_template: str = DEFAULT_TRANSLATION_FILENAME_TEMPLATE

    def __post_init__(self) -> None:
        title = str(self.attachment_title).strip()
        template = str(self.filename_template).strip()
        if not title:
            raise TranslationError("translation attachment title must not be empty")
        try:
            parsed = list(Formatter().parse(template))
        except ValueError as exc:
            raise TranslationError(
                f"invalid translation filename template: {exc}"
            ) from exc
        found_source_stem = False
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name != "source_stem":
                raise TranslationError(
                    "translation filename template only supports {source_stem}"
                )
            if format_spec or conversion:
                raise TranslationError(
                    "translation filename template does not support format specs or conversions"
                )
            found_source_stem = True
        if not found_source_stem:
            raise TranslationError(
                "translation filename template must contain {source_stem}"
            )
        object.__setattr__(self, "attachment_title", title)
        object.__setattr__(self, "filename_template", template)
        self.filename_for("source")

    def filename_for(self, source_stem: str) -> str:
        stem = str(source_stem).strip()
        if not stem:
            raise TranslationError("translation source filename stem must not be empty")
        filename = self.filename_template.format(source_stem=stem)
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or Path(filename).name != filename
        ):
            raise TranslationError(
                "translation filename template must render one PDF filename"
            )
        if not filename.casefold().endswith(".pdf"):
            raise TranslationError(
                "translation filename template must render a .pdf filename"
            )
        return filename

    def matches_attachment_title(self, title: str) -> bool:
        return title.strip() in {
            self.attachment_title,
            DEFAULT_TRANSLATION_ATTACHMENT_TITLE,
        }


DEFAULT_TRANSLATION_NAMING = TranslationNaming()


def load_translation_naming() -> TranslationNaming:
    try:
        return TranslationNaming(
            attachment_title=zotero_runtime.config_string(
                "translation", "attachment_title"
            )
            or DEFAULT_TRANSLATION_ATTACHMENT_TITLE,
            filename_template=zotero_runtime.config_string(
                "translation", "filename_template"
            )
            or DEFAULT_TRANSLATION_FILENAME_TEMPLATE,
        )
    except zotero_runtime.RuntimeConfigError as exc:
        raise TranslationError(str(exc)) from exc


def _insecure_http_allowed(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return (
        str(values.get(ALLOW_INSECURE_HTTP_ENV) or "").strip().casefold() in TRUE_VALUES
    )


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.rstrip(".").casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_transport_url(
    value: str,
    *,
    purpose: str,
    allow_loopback_http: bool = False,
    environ: Mapping[str, str] | None = None,
) -> str:
    cleaned = value.strip().rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TranslationError(f"{purpose} URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise TranslationError(f"{purpose} URL must not contain credentials")
    if parsed.scheme == "https":
        return cleaned
    if allow_loopback_http and _is_loopback_host(parsed.hostname):
        return cleaned
    if _insecure_http_allowed(environ):
        return cleaned
    raise TranslationError(
        f"{purpose} URL uses insecure remote HTTP; use HTTPS or explicitly set "
        f"{ALLOW_INSECURE_HTTP_ENV}=1"
    )


def _sensitive_values(value: Any, *, sensitive: bool = False) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            yield from _sensitive_values(
                nested,
                sensitive=sensitive
                or bool(SENSITIVE_KEY_RE.search(key_text))
                or key_text.casefold() == "apiurl",
            )
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            yield from _sensitive_values(nested, sensitive=sensitive)
        return
    if sensitive and value is not None:
        text = str(value)
        if text:
            yield text


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def state_dir() -> Path:
    override = os.environ.get("ZOTERO_TRANSLATE_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    if zotero_runtime.is_windows():
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return (
            Path(base).expanduser() / "zotero-mcp"
            if base
            else zotero_runtime.config_dir()
        )
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    return (
        Path(base).expanduser() if base else Path.home() / ".local" / "state"
    ) / "zotero-mcp"


def default_queue_path() -> Path:
    return state_dir() / "translation_queue.csv"


def default_output_dir() -> Path:
    return state_dir() / "translations"


def default_rename_watch_state_path() -> Path:
    return state_dir() / "manual_translation_rename_watch.json"


def webdav_secret_path() -> Path:
    override = zotero_runtime.configured_path(
        "ZOTERO_WEBDAV_SECRET_FILE", "translation", "webdav_secret_file"
    )
    return override or zotero_runtime.default_secret_path("translation_webdav.json")


def item_key(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    return str(data.get("key") or item.get("key") or "").upper()


def item_title(item: dict[str, Any]) -> str:
    return str((item.get("data") or {}).get("title") or "").strip()


def is_cn_pdf_attachment(
    item: dict[str, Any], naming: TranslationNaming | None = None
) -> bool:
    data = item.get("data") or {}
    title = str(data.get("title") or "").strip()
    filename = str(data.get("filename") or "")
    content_type = str(data.get("contentType") or "")
    return (naming or DEFAULT_TRANSLATION_NAMING).matches_attachment_title(title) and (
        content_type == "application/pdf" or filename.lower().endswith(".pdf")
    )


def is_pdf2zh_translation_attachment(item: dict[str, Any]) -> bool:
    data = item.get("data") or {}
    filename = str(data.get("filename") or "")
    content_type = str(data.get("contentType") or "")
    is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
    return is_pdf and bool(PDF2ZH_TRANSLATION_FILENAME_RE.search(filename))


def validate_pdf(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            header = handle.read(5)
    except OSError as exc:
        raise TranslationError(f"cannot read translated PDF: {path}: {exc}") from exc
    if header != b"%PDF-":
        raise TranslationError(f"translated output is not a PDF: {path}")


class QueueStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def lock(self) -> Iterator[None]:
        stack = ExitStack()
        try:
            stack.enter_context(
                zotero_runtime.exclusive_file_lock(self.lock_path, blocking=False)
            )
        except OSError as exc:
            raise TranslationError("translation queue is already locked") from exc
        try:
            yield
        finally:
            stack.close()

    def read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != QUEUE_FIELDS:
                raise TranslationError(
                    "queue header must be exactly: " + ",".join(QUEUE_FIELDS)
                )
            rows = [dict(row) for row in reader]
        for line, row in enumerate(rows, start=2):
            if row["status"] not in VALID_STATUSES:
                raise TranslationError(
                    f"invalid queue status at CSV line {line}: {row['status']}"
                )
            for field in ("parent_item_key", "source_attachment_key"):
                if not ZOTERO_KEY_RE.fullmatch(row[field]):
                    raise TranslationError(
                        f"invalid {field} at CSV line {line}: {row[field]}"
                    )
        return rows

    def write(self, rows: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=QUEUE_FIELDS, lineterminator="\n"
                )
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {field: row.get(field, "") for field in QUEUE_FIELDS}
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _host_path(path: Path) -> Path:
    if zotero_runtime.is_wsl() and re.match(r"^[A-Za-z]:[\\/]", str(path)):
        converted = zotero_runtime.windows_path_to_wsl_path(str(path))
        return converted or path
    return path


def _zotero_profile_roots() -> list[Path]:
    roots: list[Path] = []
    if zotero_runtime.is_windows():
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            roots.append(Path(appdata) / "Zotero" / "Zotero")
    if zotero_runtime.is_wsl():
        profile = zotero_runtime.windows_user_profile()
        if profile:
            root = zotero_runtime.windows_path_to_wsl_path(profile)
            if root:
                roots.append(root / "AppData" / "Roaming" / "Zotero" / "Zotero")
    roots.extend(
        (Path.home() / ".zotero" / "zotero", Path.home() / ".config" / "zotero")
    )
    return roots


def _profiles_from_root(root: Path) -> list[Path]:
    paths: list[Path] = []
    profiles_ini = root / "profiles.ini"
    if profiles_ini.is_file():
        parser = configparser.RawConfigParser()
        try:
            parser.read(profiles_ini, encoding="utf-8")
        except (OSError, configparser.Error):
            parser = configparser.RawConfigParser()
        configured_profiles: list[tuple[bool, Path]] = []
        for section in parser.sections():
            if not section.startswith("Profile") or not parser.has_option(
                section, "Path"
            ):
                continue
            path = Path(parser.get(section, "Path"))
            if parser.getboolean(section, "IsRelative", fallback=True):
                path = root / path
            else:
                path = _host_path(path)
            configured_profiles.append(
                (
                    parser.getboolean(section, "Default", fallback=False),
                    path / "prefs.js",
                )
            )
        configured_profiles.sort(key=lambda value: not value[0])
        paths.extend(path for _, path in configured_profiles)
    profiles_dir = root / "Profiles"
    if profiles_dir.is_dir():
        paths.extend(sorted(profiles_dir.glob("*/prefs.js")))
    return paths


def prefs_candidates(explicit: Path | None = None) -> list[Path]:
    configured = zotero_runtime.configured_path(
        "ZOTERO_PDF2ZH_PREFS", "translation", "prefs_file"
    )
    preferred = explicit or configured
    if preferred:
        return [_host_path(preferred.expanduser())]
    paths: list[Path] = []
    for root in _zotero_profile_roots():
        paths.extend(_profiles_from_root(root))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        marker = str(path).casefold()
        if marker not in seen:
            seen.add(marker)
            unique.append(path)
    return unique


def parse_pdf2zh_prefs(text: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for line in text.splitlines():
        if PREF_PREFIX not in line:
            continue
        match = PREF_RE.match(line.strip())
        if not match or not match.group("name").startswith(PREF_PREFIX):
            continue
        key = match.group("name")[len(PREF_PREFIX) :]
        try:
            values[key] = json.loads(match.group("value"))
        except json.JSONDecodeError as exc:
            raise TranslationError(
                f"invalid PDF2zh preference value for {key}"
            ) from exc
    return values


@dataclass(frozen=True)
class PDF2ZHSettings:
    prefs_path: Path
    server_url: str
    service: str
    model: str
    llm_api: dict[str, Any] | None
    request_options: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "prefs_path": str(self.prefs_path),
            "server_url": self.server_url,
            "service": self.service,
            "model": self.model,
            "llm_configured": self.llm_api is not None,
            "api_key_configured": bool(
                self.llm_api and str(self.llm_api.get("apiKey") or "").strip()
            ),
        }

    def redact(self, message: str) -> str:
        if not self.llm_api:
            return message
        redacted = message
        for value in sorted(
            set(_sensitive_values(self.llm_api)), key=len, reverse=True
        ):
            redacted = redacted.replace(value, "[redacted]")
        return redacted


def load_pdf2zh_settings(explicit: Path | None = None) -> PDF2ZHSettings:
    attempted = prefs_candidates(explicit)
    selected: Path | None = None
    values: dict[str, Any] = {}
    for path in attempted:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if PREF_PREFIX not in text:
            continue
        selected = path
        values = parse_pdf2zh_prefs(text)
        break
    if selected is None:
        locations = ", ".join(str(path) for path in attempted) or "none"
        raise TranslationError(
            "PDF2zh Zotero preferences were not found. Checked: " + locations
        )

    engine = str(values.get("engine", "pdf2zh_next") or "")
    if engine != "pdf2zh_next":
        raise TranslationError(
            "PDF2zh unattended translation requires the pdf2zh_next engine"
        )
    service = str(values.get("next_service") or "").strip()
    server_url = validate_transport_url(
        str(values.get("new_serverip", "http://localhost:8890") or ""),
        purpose="PDF2zh Server",
        allow_loopback_http=True,
    )
    if not service:
        raise TranslationError("PDF2zh next_service is not configured in Zotero")
    raw_apis = values.get("llmApis")
    apis: list[Any] = []
    if isinstance(raw_apis, str) and raw_apis.strip():
        try:
            parsed = json.loads(raw_apis)
        except json.JSONDecodeError as exc:
            raise TranslationError("PDF2zh llmApis preference is invalid JSON") from exc
        if isinstance(parsed, list):
            apis = parsed
    active = [
        value
        for value in apis
        if isinstance(value, dict)
        and value.get("activate") is True
        and str(value.get("service") or "") == service
    ]
    if len(active) > 1:
        raise TranslationError(f"multiple active PDF2zh configurations for {service}")
    if not active and service not in FREE_SERVICES:
        raise TranslationError(f"no active PDF2zh configuration for {service}")

    llm_api: dict[str, Any] | None = None
    model = ""
    if active:
        entry = active[0]
        model = str(entry.get("model") or "").strip()
        llm_api = {
            "service": service,
            "model": model,
            "apiKey": str(entry.get("apiKey") or ""),
            "apiUrl": str(entry.get("apiUrl") or ""),
            "extraData": entry.get("extraData")
            if isinstance(entry.get("extraData"), dict)
            else {},
        }
    options = {key: values[key] for key in REQUEST_PREFS if key in values}
    return PDF2ZHSettings(
        prefs_path=selected,
        server_url=server_url,
        service=service,
        model=model,
        llm_api=llm_api,
        request_options=options,
    )


def server_url_candidates(server_url: str) -> list[str]:
    candidates = [server_url.rstrip("/")]
    parsed = urlsplit(server_url)
    if zotero_runtime.is_wsl() and parsed.hostname in {"127.0.0.1", "localhost"}:
        gateway = zotero_local.wsl_gateway_ip()
        if gateway:
            netloc = gateway + (f":{parsed.port}" if parsed.port else "")
            candidates.append(
                urlunsplit((parsed.scheme, netloc, parsed.path, "", "")).rstrip("/")
            )
    return list(dict.fromkeys(candidates))


def build_translation_payload(
    file_name: str,
    file_content: str,
    settings: PDF2ZHSettings,
    qps: int,
    pool_size: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fileName": file_name,
        "fileContent": file_content,
        "engine": "pdf2zh_next",
        "next_service": settings.service,
        **settings.request_options,
        "qps": qps,
        "poolSize": pool_size,
        "mono": True,
        "dual": False,
        "noMono": False,
        "noDual": True,
        "noWatermark": True,
    }
    if settings.llm_api is not None:
        payload["llm_api"] = settings.llm_api
    return payload


class PDF2ZHClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        output_dir: Path | None = None,
        naming: TranslationNaming | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.output_dir = output_dir or default_output_dir()
        self.naming = naming or DEFAULT_TRANSLATION_NAMING
        self.base_url = ""

    def health(self, settings: PDF2ZHSettings) -> str:
        failures: list[str] = []
        for base_url in server_url_candidates(settings.server_url):
            try:
                response = self.session.get(f"{base_url}/health", timeout=10)
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError) as exc:
                failures.append(f"{base_url}: {type(exc).__name__}")
                continue
            if isinstance(data, dict) and data.get("status") == "ok":
                self.base_url = base_url
                return base_url
            failures.append(f"{base_url}: invalid health response")
        raise TranslationError(
            "PDF2zh Server health check failed: " + "; ".join(failures)
        )

    @staticmethod
    def input_filename(parent_key: str, source_key: str, source_pdf: Path) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source_pdf.stem).strip("._")
        return f"{parent_key}_{source_key}_{(stem[:80] or 'paper')}.pdf"

    def translate(
        self,
        settings: PDF2ZHSettings,
        parent_key: str,
        source_key: str,
        source_pdf: Path,
        qps: int,
        pool_size: int,
    ) -> Path:
        if not self.base_url:
            self.health(settings)
        input_name = self.input_filename(parent_key, source_key, source_pdf)
        payload = build_translation_payload(
            input_name,
            base64.b64encode(source_pdf.read_bytes()).decode("ascii"),
            settings,
            qps,
            pool_size,
        )
        response = self.session.post(
            f"{self.base_url}/translate",
            json=payload,
            timeout=(10, 12 * 60 * 60),
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TranslationError(
                f"PDF2zh Server returned HTTP {response.status_code} without JSON"
            ) from exc
        if not isinstance(data, dict):
            raise TranslationError(
                f"PDF2zh Server returned HTTP {response.status_code} with invalid JSON data"
            )
        if not response.ok or data.get("status") != "success":
            message = settings.redact(
                str(data.get("message") or f"HTTP {response.status_code}")
            )
            raise TranslationError(f"PDF2zh translation failed: {message}")
        files = data.get("fileList")
        mono = (
            [
                name
                for name in files
                if isinstance(name, str) and name.lower().endswith(".mono.pdf")
            ]
            if isinstance(files, list)
            else []
        )
        if len(mono) != 1:
            raise TranslationError(f"PDF2zh returned unexpected files: {files!r}")

        item_dir = self.output_dir / f"{parent_key}_{source_key}"
        item_dir.mkdir(parents=True, exist_ok=True)
        output = item_dir / self.naming.filename_for(source_pdf.stem)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            download = self.session.get(
                f"{self.base_url}/translatedFile/{quote(mono[0], safe='')}",
                timeout=(10, 30 * 60),
                stream=True,
            )
            download.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in download.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            validate_pdf(temporary)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return output


class ZoteroClient:
    def __init__(self, naming: TranslationNaming | None = None) -> None:
        self.naming = naming or DEFAULT_TRANSLATION_NAMING

    def parent_item(self, key: str) -> dict[str, Any]:
        item = zotero_local.get_item(key)
        parent = zotero_local.resolve_top_level_item(item)
        if not item_key(parent):
            raise TranslationError(f"Zotero item has no key: {key}")
        return parent

    def children(self, parent_key: str) -> list[dict[str, Any]]:
        return zotero_local.get_children(parent_key)

    def cn_attachment(self, parent_key: str) -> dict[str, Any] | None:
        return next(
            (
                child
                for child in self.children(parent_key)
                if is_cn_pdf_attachment(child, self.naming)
            ),
            None,
        )

    def select_source_attachment(self, parent: dict[str, Any]) -> dict[str, Any]:
        try:
            return zotero_local.english_pdf_attachment_for_item(parent)
        except SystemExit as exc:
            raise TranslationError(str(exc)) from exc

    def source_pdf(self, parent_key: str, attachment_key: str) -> Path:
        attachment = zotero_local.get_item(attachment_key)
        data = attachment.get("data") or {}
        if str(data.get("parentItem") or "") != parent_key:
            raise TranslationError(
                f"source attachment {attachment_key} is not attached to {parent_key}"
            )
        pdfs = zotero_local.find_pdf_for_attachment(attachment_key)
        if not pdfs:
            raise TranslationError(
                f"source PDF is missing for attachment {attachment_key}"
            )
        return pdfs[0]

    def attachment_pdf(self, attachment_key: str) -> Path:
        pdfs = zotero_local.find_pdf_for_attachment(attachment_key)
        if len(pdfs) != 1:
            raise TranslationError(
                f"attachment {attachment_key} must resolve to exactly one local PDF"
            )
        return pdfs[0]

    def collection_item_keys(
        self, reference: str, recursive: bool, limit: int
    ) -> list[str]:
        collections = zotero_local.fetch_all_collections()
        if ZOTERO_KEY_RE.fullmatch(reference.upper()):
            request = {"key": reference.upper()}
        elif ">" in reference:
            request = {"path": reference}
        else:
            request = {"name": reference}
        resolved = resolve_collection(request, collections=collections)
        listing = zotero_local.list_collection_items(
            resolved["key"],
            recursive=recursive,
            limit=limit,
            collections=collections,
        )
        return [str(row["key"]) for row in listing["items"]]


def _translation_rename_records(
    item_keys: list[str],
    *,
    zotero: ZoteroClient,
    api: Any,
    naming: TranslationNaming,
) -> tuple[int, list[dict[str, Any]]]:
    if not isinstance(item_keys, list) or not 1 <= len(item_keys) <= 50:
        raise TranslationError("item_keys must contain between 1 and 50 Zotero keys")
    normalized = []
    for value in item_keys:
        key = str(value or "").strip().upper()
        if not ZOTERO_KEY_RE.fullmatch(key):
            raise TranslationError(f"invalid Zotero item key: {value}")
        normalized.append(key)

    status = api.web_api_status()
    user_id = status.get("user_id")
    if not isinstance(user_id, int) or user_id < 1:
        raise TranslationError("Zotero Web API returned an invalid user_id")

    results = []
    seen_parents: set[str] = set()
    for requested_key in normalized:
        parent = zotero.parent_item(requested_key)
        parent_key = item_key(parent)
        if parent_key in seen_parents:
            continue
        seen_parents.add(parent_key)
        source = zotero.select_source_attachment(parent)
        source_key = str(source.get("key") or "").upper()
        source_filename = str(source.get("filename") or "")
        if not ZOTERO_KEY_RE.fullmatch(source_key) or not source_filename:
            raise TranslationError(
                f"invalid English source attachment for {parent_key}"
            )
        target_filename = naming.filename_for(Path(source_filename).stem)
        candidates = [
            child
            for child in zotero.children(parent_key)
            if is_cn_pdf_attachment(child, naming)
            or is_pdf2zh_translation_attachment(child)
        ]
        row: dict[str, Any] = {
            "requested_key": requested_key,
            "parent_item_key": parent_key,
            "paper_title": item_title(parent),
            "source_attachment_key": source_key,
            "source_filename": source_filename,
            "new_title": naming.attachment_title,
            "new_filename": target_filename,
            "status": "blocked",
            "blockers": [],
        }
        if not candidates:
            row["blockers"] = ["no_translation_attachment"]
            results.append(row)
            continue
        if len(candidates) != 1:
            row["blockers"] = ["multiple_translation_attachments"]
            row["candidate_keys"] = [item_key(candidate) for candidate in candidates]
            results.append(row)
            continue

        candidate = candidates[0]
        data = candidate.get("data") or {}
        attachment_key = item_key(candidate)
        old_title = str(data.get("title") or "")
        old_filename = str(data.get("filename") or "")
        row.update(
            {
                "translation_attachment_key": attachment_key,
                "old_title": old_title,
                "old_filename": old_filename,
            }
        )
        if not ZOTERO_KEY_RE.fullmatch(attachment_key):
            row["blockers"] = ["invalid_translation_attachment_key"]
            results.append(row)
            continue
        try:
            local_pdf = zotero.attachment_pdf(attachment_key)
        except TranslationError as exc:
            row["blockers"] = [str(exc)]
            results.append(row)
            continue
        row["local_pdf"] = str(local_pdf)
        if local_pdf.name != old_filename:
            row["blockers"] = ["local_file_metadata_mismatch"]
            results.append(row)
            continue
        target_path = local_pdf.with_name(target_filename)
        if target_path != local_pdf and target_path.exists():
            row["blockers"] = ["target_filename_exists"]
            results.append(row)
            continue

        cloud = api.web_api_get_item(user_id, attachment_key)
        cloud_data = cloud.get("data") or {}
        version = cloud.get("version", cloud_data.get("version"))
        matches = (
            str(cloud_data.get("parentItem") or "") == parent_key
            and str(cloud_data.get("title") or "") == old_title
            and str(cloud_data.get("filename") or "") == old_filename
        )
        if not matches or not isinstance(version, int) or version < 1:
            row["blockers"] = ["local_cloud_metadata_mismatch"]
            results.append(row)
            continue
        row["version"] = version
        row["blockers"] = []
        row["status"] = (
            "unchanged"
            if old_title == naming.attachment_title and old_filename == target_filename
            else "rename"
        )
        results.append(row)
    return user_id, results


def plan_manual_translation_renames(
    item_keys: list[str],
    *,
    zotero: ZoteroClient | None = None,
    api: Any = zotero_web_api,
    naming: TranslationNaming | None = None,
) -> dict[str, Any]:
    selected_naming = naming or load_translation_naming()
    user_id, results = _translation_rename_records(
        item_keys,
        zotero=zotero or ZoteroClient(naming=selected_naming),
        api=api,
        naming=selected_naming,
    )
    return {
        "user_id": user_id,
        "requested": len(item_keys),
        "rename": sum(row["status"] == "rename" for row in results),
        "unchanged": sum(row["status"] == "unchanged" for row in results),
        "blocked": sum(row["status"] == "blocked" for row in results),
        "results": results,
    }


def apply_manual_translation_renames(
    items: list[dict[str, Any]],
    confirm: bool,
    *,
    zotero: ZoteroClient | None = None,
    api: Any = zotero_web_api,
    naming: TranslationNaming | None = None,
) -> dict[str, Any]:
    if confirm is not True:
        raise TranslationError("confirm=true is required for attachment renaming")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise TranslationError("items must contain between 1 and 50 rename records")
    required = {
        "parent_item_key",
        "source_attachment_key",
        "translation_attachment_key",
        "new_title",
        "new_filename",
    }
    requested = []
    for position, record in enumerate(items, start=1):
        if not isinstance(record, dict) or not required.issubset(record):
            raise TranslationError(f"item {position} is missing reviewed rename fields")
        requested.append(str(record["parent_item_key"]).strip().upper())

    selected_naming = naming or load_translation_naming()
    client = zotero or ZoteroClient(naming=selected_naming)
    user_id, plan = _translation_rename_records(
        requested, zotero=client, api=api, naming=selected_naming
    )
    by_parent = {row["parent_item_key"]: row for row in plan}
    results = []
    for record in items:
        parent_key = str(record["parent_item_key"]).strip().upper()
        current = by_parent.get(parent_key)
        if current is None or current["status"] not in {"rename", "unchanged"}:
            raise TranslationError(f"attachment rename is blocked for {parent_key}")
        for field in ("source_attachment_key", "translation_attachment_key"):
            if str(record[field]).strip().upper() != current[field]:
                raise TranslationError(
                    f"{field} changed for {parent_key}; run plan again"
                )
        for field in ("new_title", "new_filename"):
            if str(record[field]) != current[field]:
                raise TranslationError(
                    f"{field} changed for {parent_key}; run plan again"
                )
        if current["status"] == "unchanged":
            results.append({**current, "status": "unchanged"})
            continue

        attachment_key = current["translation_attachment_key"]
        response = api.web_api_request(
            "PATCH",
            f"users/{user_id}/items/{attachment_key}",
            payload={
                "title": current["new_title"],
                "filename": current["new_filename"],
            },
            headers={"If-Unmodified-Since-Version": str(current["version"])},
            timeout=30.0,
        )
        if response.status_code in {409, 412}:
            raise zotero_web_api.ZoteroVersionConflictError(
                f"attachment {attachment_key} changed before rename; run plan again"
            )
        if not response.ok:
            raise zotero_web_api.web_api_error(response)
        verified = api.web_api_get_item(user_id, attachment_key)
        verified_data = verified.get("data") or {}
        if (
            str(verified_data.get("parentItem") or "") != parent_key
            or str(verified_data.get("title") or "") != current["new_title"]
            or str(verified_data.get("filename") or "") != current["new_filename"]
        ):
            raise TranslationError(
                f"Zotero attachment {attachment_key} failed rename read-back"
            )
        results.append({**current, "status": "renamed", "local_sync_pending": True})
    return {
        "renamed": sum(row["status"] == "renamed" for row in results),
        "unchanged": sum(row["status"] == "unchanged" for row in results),
        "results": results,
    }


def _attachment_versions(since: int | None = None) -> dict[str, int]:
    versions: dict[str, int] = {}
    start = 0
    while True:
        params: dict[str, Any] = {
            "itemType": "attachment",
            "format": "versions",
            "start": start,
            "limit": 100,
        }
        if since is not None:
            params["since"] = since
        page = zotero_local.zotero_get("users/0/items", params) or {}
        if not isinstance(page, dict):
            raise TranslationError("Zotero attachment versions response is invalid")
        for key, version in page.items():
            normalized = str(key).upper()
            if ZOTERO_KEY_RE.fullmatch(normalized) and isinstance(version, int):
                versions[normalized] = version
        if len(page) < 100:
            break
        start += len(page)
    return versions


def _load_rename_watch_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TranslationError(f"cannot read rename watch state: {path}") from exc
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("last_version"), int)
        or not isinstance(state.get("pending"), dict)
    ):
        raise TranslationError(f"invalid rename watch state: {path}")
    return state


def _save_rename_watch_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _auto_rename_attachment(
    attachment_key: str,
    *,
    zotero: ZoteroClient | None = None,
    api: Any = zotero_web_api,
    naming: TranslationNaming | None = None,
) -> dict[str, Any]:
    selected_naming = naming or load_translation_naming()
    attachment = zotero_local.get_item(attachment_key)
    if is_cn_pdf_attachment(attachment, selected_naming):
        return {"status": "already_named", "attachment_key": attachment_key}
    if not is_pdf2zh_translation_attachment(attachment):
        return {"status": "ignored", "attachment_key": attachment_key}
    parent_key = str((attachment.get("data") or {}).get("parentItem") or "").upper()
    if not ZOTERO_KEY_RE.fullmatch(parent_key):
        return {"status": "blocked", "blockers": ["invalid_parent_item_key"]}
    plan = plan_manual_translation_renames(
        [parent_key], zotero=zotero, api=api, naming=selected_naming
    )
    row = plan["results"][0]
    if row["status"] == "blocked":
        return row
    result = apply_manual_translation_renames(
        [
            {
                "parent_item_key": row["parent_item_key"],
                "source_attachment_key": row["source_attachment_key"],
                "translation_attachment_key": row["translation_attachment_key"],
                "new_title": row["new_title"],
                "new_filename": row["new_filename"],
            }
        ],
        True,
        zotero=zotero,
        api=api,
        naming=selected_naming,
    )
    return result["results"][0]


def scan_manual_translation_renames(
    state_path: Path | None = None,
    *,
    now: float | None = None,
    versions_reader: Any = _attachment_versions,
    rename_one: Any = _auto_rename_attachment,
) -> dict[str, Any]:
    path = (state_path or default_rename_watch_state_path()).expanduser()
    state = _load_rename_watch_state(path)
    if state is None:
        versions = versions_reader()
        state = {"last_version": max(versions.values(), default=0), "pending": {}}
        _save_rename_watch_state(path, state)
        return {
            "initialized": True,
            "baseline_version": state["last_version"],
            "renamed": 0,
            "pending": 0,
        }

    current_time = time.time() if now is None else now
    changes = versions_reader(state["last_version"])
    pending = state["pending"]
    for key, version in changes.items():
        previous = pending.get(key)
        if not isinstance(previous, dict) or version > int(previous.get("version", 0)):
            pending[key] = {
                "version": version,
                "attempts": 0,
                "next_attempt_at": 0,
                "last_error": "",
            }
    if changes:
        state["last_version"] = max(state["last_version"], *changes.values())
    _save_rename_watch_state(path, state)

    counts = {"renamed": 0, "already_named": 0, "ignored": 0, "blocked": 0}
    results = []
    for key in sorted(pending, key=lambda value: pending[value]["version"]):
        record = pending[key]
        if float(record.get("next_attempt_at", 0)) > current_time:
            continue
        try:
            result = rename_one(key)
        except Exception as exc:  # noqa: BLE001 - retain item for a later sync retry
            status = "blocked"
            error = str(exc)[:MAX_ERROR_LENGTH]
            result = {"status": status, "attachment_key": key, "error": error}
        status = str(result.get("status") or "blocked")
        if status in {"renamed", "unchanged", "already_named", "ignored"}:
            counts["already_named" if status == "unchanged" else status] += 1
            del pending[key]
        else:
            counts["blocked"] += 1
            attempts = int(record.get("attempts", 0)) + 1
            record["attempts"] = attempts
            record["last_error"] = str(
                result.get("error") or ",".join(result.get("blockers") or [])
            )[:MAX_ERROR_LENGTH]
            record["next_attempt_at"] = current_time + min(
                30 * (2 ** (attempts - 1)), 600
            )
        results.append(result)
    _save_rename_watch_state(path, state)
    return {
        "initialized": False,
        "last_version": state["last_version"],
        **counts,
        "pending": len(pending),
        "results": results,
    }


def watch_manual_translation_renames(
    *,
    once: bool = False,
    state_path: Path | None = None,
) -> dict[str, Any]:
    path = (state_path or default_rename_watch_state_path()).expanduser()
    lock_path = path.with_name(path.name + ".lock")
    try:
        with zotero_runtime.exclusive_file_lock(lock_path, blocking=False):
            last_result: dict[str, Any] = {"stopped": "disabled"}
            while True:
                enabled = zotero_runtime.config_bool(
                    "translation", "auto_rename_manual", default=False
                )
                if not enabled:
                    if once:
                        return last_result
                    interval = (
                        zotero_runtime.config_positive_int(
                            "translation", "rename_poll_seconds"
                        )
                        or 30
                    )
                    time.sleep(interval)
                    continue
                try:
                    last_result = scan_manual_translation_renames(path)
                except requests.RequestException as exc:
                    if once:
                        raise
                    last_result = {"error": str(exc)[:MAX_ERROR_LENGTH]}
                if once:
                    return last_result
                if last_result.get("initialized") or any(
                    last_result.get(name) for name in ("renamed", "blocked", "error")
                ):
                    print(json.dumps(last_result, ensure_ascii=False), flush=True)
                interval = (
                    zotero_runtime.config_positive_int(
                        "translation", "rename_poll_seconds"
                    )
                    or 30
                )
                time.sleep(interval)
    except BlockingIOError as exc:
        raise TranslationError(
            "manual translation rename watch is already running"
        ) from exc


def configure_rename_watch_service(action: str) -> dict[str, Any]:
    if action not in {"install", "status", "remove"}:
        raise TranslationError(f"unknown rename watch service action: {action}")
    command = [
        str(Path(sys.executable).resolve()),
        "-m",
        "zotero_mcp.zotero_translate",
        "rename-watch",
    ]
    if zotero_runtime.is_windows():
        scheduler = shutil.which("schtasks.exe") or shutil.which("schtasks")
        if not scheduler:
            raise TranslationError("schtasks.exe is not available")
        task_name = "Zotero MCP Manual Translation Rename"
        if action == "install":
            calls = [
                [
                    scheduler,
                    "/Create",
                    "/F",
                    "/SC",
                    "ONLOGON",
                    "/TN",
                    task_name,
                    "/TR",
                    subprocess.list2cmdline(command),
                ],
                [scheduler, "/Run", "/TN", task_name],
            ]
        elif action == "remove":
            calls = [[scheduler, "/Delete", "/F", "/TN", task_name]]
        else:
            calls = [[scheduler, "/Query", "/TN", task_name]]
        output = ""
        for call in calls:
            result = subprocess.run(call, capture_output=True, text=True, check=False)
            output = (result.stdout or result.stderr).strip()
            if result.returncode != 0:
                raise TranslationError(
                    f"Windows Task Scheduler failed with code {result.returncode}: {output}"
                )
        return {
            "action": action,
            "scheduler": "windows-task-scheduler",
            "output": output,
        }

    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise TranslationError("systemctl is not available")
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_root = (
        Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    )
    unit_path = (
        config_root / "systemd" / "user" / f"{RENAME_WATCH_SERVICE_NAME}.service"
    )
    if action == "install":
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit = "\n".join(
            [
                "[Unit]",
                "Description=Rename manually translated Zotero PDF attachments",
                "After=default.target",
                "",
                "[Service]",
                "Type=simple",
                f"ExecStart={shlex.join(command)}",
                "Restart=on-failure",
                "RestartSec=15",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        temporary = unit_path.with_name(f".{unit_path.name}.{os.getpid()}.tmp")
        temporary.write_text(unit, encoding="utf-8")
        os.replace(temporary, unit_path)
        calls = [
            [systemctl, "--user", "daemon-reload"],
            [systemctl, "--user", "enable", "--now", unit_path.name],
        ]
    elif action == "remove":
        calls = [
            [systemctl, "--user", "disable", "--now", unit_path.name],
        ]
    else:
        calls = [[systemctl, "--user", "is-active", unit_path.name]]
    output = ""
    for call in calls:
        result = subprocess.run(call, capture_output=True, text=True, check=False)
        output = (result.stdout or result.stderr).strip()
        if result.returncode != 0 and not (
            action == "remove" and result.returncode == 5
        ):
            raise TranslationError(
                f"systemd user service failed with code {result.returncode}: {output}"
            )
    if action == "remove":
        unit_path.unlink(missing_ok=True)
        subprocess.run(
            [systemctl, "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            check=False,
        )
    return {
        "action": action,
        "scheduler": "systemd-user-service",
        "unit": str(unit_path),
        "output": output,
    }


def _private_json(path: Path) -> dict[str, Any]:
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise TranslationError(f"secret file permissions are too broad: {path}")
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise TranslationError(f"cannot read WebDAV secret file: {path}") from exc
    if not isinstance(data, dict):
        raise TranslationError(f"WebDAV secret file is not a JSON object: {path}")
    return data


def load_webdav_config(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str, str, float, str]:
    values = os.environ if environ is None else environ
    secret_path = webdav_secret_path()
    stored = _private_json(secret_path)
    url = str(values.get("ZOTERO_WEBDAV_URL") or stored.get("url") or "").strip()
    username = str(
        values.get("ZOTERO_WEBDAV_USERNAME") or stored.get("username") or ""
    ).strip()
    password = str(values.get("ZOTERO_WEBDAV_PASSWORD") or stored.get("password") or "")
    missing = [
        name
        for name, value in (
            ("ZOTERO_WEBDAV_URL", url),
            ("ZOTERO_WEBDAV_USERNAME", username),
            ("ZOTERO_WEBDAV_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise TranslationError("missing WebDAV configuration: " + ", ".join(missing))
    url = validate_transport_url(
        url,
        purpose="WebDAV",
        environ=values,
    )
    timeout_raw = str(
        values.get("ZOTERO_WEBDAV_TIMEOUT")
        or stored.get("timeout")
        or DEFAULT_WEBDAV_TIMEOUT
    )
    try:
        timeout = float(timeout_raw)
    except ValueError as exc:
        raise TranslationError("ZOTERO_WEBDAV_TIMEOUT must be positive") from exc
    if timeout <= 0:
        raise TranslationError("ZOTERO_WEBDAV_TIMEOUT must be positive")
    source = (
        "environment"
        if all(values.get(name) for name in WEBDAV_ENV_NAMES)
        else str(secret_path)
    )
    return url.rstrip("/") + "/", username, password, timeout, source


class ZoteroAttachmentClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        api: Any = zotero_web_api,
        environ: Mapping[str, str] | None = None,
        naming: TranslationNaming | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.api = api
        self.environ = environ
        self.naming = naming or DEFAULT_TRANSLATION_NAMING

    def configuration_status(self) -> dict[str, Any]:
        _, _, _, timeout, source = load_webdav_config(self.environ)
        return {"configured": True, "source": source, "timeout": timeout}

    def _config(self) -> tuple[str, str, str, float]:
        url, username, password, timeout, _ = load_webdav_config(self.environ)
        return url, username, password, timeout

    def _verify_webdav_write(self, base_url: str, timeout: float) -> None:
        probe_url = f"{base_url}zotero-mcp-write-test-{uuid.uuid4().hex}.tmp"
        put_error: TranslationError | None = None
        try:
            response = self.session.put(
                probe_url,
                data=b"",
                headers={"Content-Type": "application/octet-stream"},
                timeout=(10.0, min(timeout, 30.0)),
            )
            if response.status_code not in {200, 201, 204}:
                put_error = TranslationError(
                    f"WebDAV write check failed with HTTP {response.status_code}"
                )
        except requests.RequestException as exc:
            put_error = TranslationError(
                f"WebDAV write check failed: {type(exc).__name__}: {exc}"
            )

        try:
            cleanup = self.session.delete(
                probe_url,
                timeout=(10.0, min(timeout, 30.0)),
            )
        except requests.RequestException as exc:
            raise TranslationError(
                f"WebDAV write-check cleanup failed: {type(exc).__name__}: {exc}"
            ) from exc
        if cleanup.status_code not in {200, 204, 404}:
            raise TranslationError(
                f"WebDAV write-check cleanup failed with HTTP {cleanup.status_code}"
            )
        if put_error is not None:
            raise put_error

    def preflight(self, *, verify_write: bool = False) -> int:
        status = self.api.web_api_status()
        user_id = status.get("user_id")
        if not isinstance(user_id, int) or user_id < 1:
            raise TranslationError("Zotero Web API returned an invalid user_id")
        if status.get("files_write") is not True:
            raise TranslationError("Zotero Web API key lacks file write access")
        base_url, username, password, timeout = self._config()
        self.session.auth = (username, password)
        response = self.session.request(
            "PROPFIND",
            base_url,
            headers={"Depth": "0"},
            timeout=(10.0, min(timeout, 30.0)),
        )
        if response.status_code not in {200, 204, 207}:
            raise TranslationError(
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
                raise TranslationError("Zotero Web API children response was invalid")
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
            raise TranslationError(
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
                raise TranslationError(
                    f"Zotero rejected attachment creation: {failure}"
                )
            raise TranslationError(
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
            raise TranslationError(
                f"Zotero attachment {attachment_key} failed read-back: "
                + ", ".join(failed)
            )
        version = item.get("version", data.get("version"))
        if not isinstance(version, int) or version < 1:
            raise TranslationError(f"Zotero attachment {attachment_key} has no version")
        return version

    @staticmethod
    def _same_file(
        item: dict[str, Any],
        parent_key: str,
        filename: str,
        md5_hex: str,
        mtime_ms: int,
    ) -> bool:
        data = item.get("data") or {}
        try:
            existing_mtime = int(data.get("mtime"))
        except (TypeError, ValueError):
            return False
        return (
            str(data.get("parentItem") or "") == parent_key
            and str(data.get("linkMode") or "") == "imported_file"
            and str(data.get("filename") or "") == filename
            and str(data.get("md5") or "").lower() == md5_hex
            and existing_mtime == mtime_ms
        )

    def _upload_webdav(
        self, attachment_key: str, file_path: Path, md5_hex: str, mtime_ms: int
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
                handle.write(file_path, arcname=file_path.name)
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
                raise TranslationError("attachment version is unavailable")
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
        self, user_id: int, parent_key: str, file_path: Path
    ) -> dict[str, Any]:
        validate_pdf(file_path)
        parent = self.api.web_api_get_item(user_id, parent_key)
        data = parent.get("data") or {}
        if data.get("itemType") in {"attachment", "annotation", "note"} or data.get(
            "parentItem"
        ):
            raise TranslationError(f"Zotero parent is not top-level: {parent_key}")
        md5_hex = self._file_md5(file_path)
        mtime_ms = int(file_path.stat().st_mtime * 1000)
        existing_matches = [
            child
            for child in self._cloud_children(user_id, parent_key)
            if is_cn_pdf_attachment(child, self.naming)
        ]
        if len(existing_matches) > 1:
            raise TranslationError(
                f"multiple existing translation attachments for {parent_key}"
            )
        existing = existing_matches[0] if existing_matches else None
        if existing:
            existing_key = item_key(existing)
            if not ZOTERO_KEY_RE.fullmatch(existing_key):
                raise TranslationError(
                    "existing translation attachment has no valid Zotero key"
                )
            refreshed = False
            if self._same_file(existing, parent_key, file_path.name, md5_hex, mtime_ms):
                self._upload_webdav(existing_key, file_path, md5_hex, mtime_ms)
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
            "title": self.naming.attachment_title,
            "parentItem": parent_key,
            "contentType": "application/pdf",
            "charset": "",
            "filename": file_path.name,
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
                file_path.name,
                md5_hex,
                mtime_ms,
                self.naming.attachment_title,
            )
            self._upload_webdav(attachment_key, file_path, md5_hex, mtime_ms)
            verified = self.api.web_api_get_item(user_id, attachment_key)
            self._validate_attachment(
                verified,
                attachment_key,
                parent_key,
                file_path.name,
                md5_hex,
                mtime_ms,
                self.naming.attachment_title,
            )
        except Exception as exc:
            if attachment_key:
                cleanup_errors = self._cleanup(user_id, attachment_key, version)
                if cleanup_errors:
                    raise TranslationError(
                        f"attachment import failed for {attachment_key}: {exc}; cleanup incomplete: "
                        + "; ".join(cleanup_errors)
                    ) from exc
            if isinstance(exc, TranslationError):
                raise
            raise TranslationError(f"Zotero attachment import failed: {exc}") from exc
        return {
            "ok": True,
            "already_present": False,
            "attachment_key": attachment_key,
            "title": self.naming.attachment_title,
        }


class TranslationWorker:
    def __init__(
        self,
        store: QueueStore,
        zotero: ZoteroClient | None = None,
        server: PDF2ZHClient | None = None,
        attachments: ZoteroAttachmentClient | None = None,
        prefs_path: Path | None = None,
        naming: TranslationNaming | None = None,
    ) -> None:
        selected_naming = naming or load_translation_naming()
        self.store = store
        self.naming = selected_naming
        self.zotero = zotero or ZoteroClient(naming=selected_naming)
        self.server = server or PDF2ZHClient(naming=selected_naming)
        self.attachments = attachments or ZoteroAttachmentClient(naming=selected_naming)
        self.prefs_path = prefs_path

    def enqueue(self, keys: list[str]) -> dict[str, int]:
        with self.store.lock():
            rows = self.store.read()
            known = {row["parent_item_key"] for row in rows}
            added = skipped = 0
            for requested_key in keys:
                parent = self.zotero.parent_item(requested_key)
                parent_key = item_key(parent)
                if parent_key in known:
                    skipped += 1
                    continue
                source = self.zotero.select_source_attachment(parent)
                rows.append(
                    {
                        "paper_title": item_title(parent),
                        "parent_item_key": parent_key,
                        "source_attachment_key": str(source["key"]),
                        "status": "done"
                        if self.zotero.cn_attachment(parent_key)
                        else "pending",
                        "output_pdf": "",
                        "last_error": "",
                    }
                )
                known.add(parent_key)
                added += 1
            self.store.write(rows)
        return {"added": added, "skipped": skipped, "total": len(rows)}

    def enqueue_collection(
        self, reference: str, recursive: bool, limit: int
    ) -> dict[str, int]:
        return self.enqueue(
            self.zotero.collection_item_keys(reference, recursive, limit)
        )

    def retry(self, keys: list[str]) -> dict[str, int]:
        requested = {key.upper() for key in keys}
        with self.store.lock():
            rows = self.store.read()
            reset = 0
            for row in rows:
                if row["parent_item_key"] in requested and row["status"] == "failed":
                    row["status"] = "pending"
                    row["last_error"] = ""
                    reset += 1
            self.store.write(rows)
        return {"reset": reset}

    def run_batch(
        self, qps: int, pool_size: int, max_items: int, dry_run: bool = False
    ) -> dict[str, Any]:
        with self.store.lock():
            rows = self.store.read()
            recovered = 0
            if not dry_run:
                for row in rows:
                    if row["status"] in {"translating", "importing"}:
                        previous = row["status"]
                        row["status"] = "failed"
                        row["last_error"] = (
                            f"previous worker stopped while status={previous}; inspect and retry"
                        )
                        recovered += 1
                if recovered:
                    self.store.write(rows)
            selected = [
                index for index, row in enumerate(rows) if row["status"] == "pending"
            ][:max_items]
            if not selected:
                return {
                    "dry_run": dry_run,
                    "selected": 0,
                    "done": 0,
                    "failed": 0,
                    "recovered_as_failed": recovered,
                }

            settings = load_pdf2zh_settings(self.prefs_path)
            server_url = self.server.health(settings)
            user_id = self.attachments.preflight(verify_write=not dry_run)
            if dry_run:
                return self._dry_run(
                    rows, selected, settings, server_url, user_id, qps, pool_size
                )

            done = failed = 0
            for index in selected:
                try:
                    self._process_row(rows, index, settings, user_id, qps, pool_size)
                except Exception as exc:  # noqa: BLE001 - one row must not stop the batch
                    rows[index]["status"] = "failed"
                    rows[index]["last_error"] = settings.redact(str(exc))[
                        :MAX_ERROR_LENGTH
                    ]
                    self.store.write(rows)
                    failed += 1
                else:
                    done += 1
            return {
                "selected": len(selected),
                "done": done,
                "failed": failed,
                "recovered_as_failed": recovered,
                "service": settings.service,
                "model": settings.model,
            }

    def _dry_run(
        self,
        rows: list[dict[str, str]],
        selected: list[int],
        settings: PDF2ZHSettings,
        server_url: str,
        user_id: int,
        qps: int,
        pool_size: int,
    ) -> dict[str, Any]:
        items = []
        for index in selected:
            row = rows[index]
            source_pdf = self.zotero.source_pdf(
                row["parent_item_key"], row["source_attachment_key"]
            )
            output = Path(row["output_pdf"]) if row["output_pdf"] else None
            items.append(
                {
                    "parent_item_key": row["parent_item_key"],
                    "source_attachment_key": row["source_attachment_key"],
                    "source_pdf": str(source_pdf),
                    "cn_already_present": bool(
                        self.zotero.cn_attachment(row["parent_item_key"])
                    ),
                    "local_translation_ready": bool(output and output.is_file()),
                }
            )
        return {
            "dry_run": True,
            "selected": len(selected),
            "user_id": user_id,
            "server_url": server_url,
            "service": settings.service,
            "model": settings.model,
            "qps": qps,
            "pool_size": pool_size,
            "items": items,
        }

    def _process_row(
        self,
        rows: list[dict[str, str]],
        index: int,
        settings: PDF2ZHSettings,
        user_id: int,
        qps: int,
        pool_size: int,
    ) -> None:
        row = rows[index]
        parent_key = row["parent_item_key"]
        source_key = row["source_attachment_key"]
        output = Path(row["output_pdf"]) if row["output_pdf"] else None
        if self.zotero.cn_attachment(parent_key) and not (output and output.is_file()):
            row["status"] = "done"
            row["last_error"] = ""
            self.store.write(rows)
            return
        source_pdf = self.zotero.source_pdf(parent_key, source_key)
        if output and output.is_file():
            validate_pdf(output)
        else:
            row["status"] = "translating"
            row["last_error"] = ""
            self.store.write(rows)
            output = self.server.translate(
                settings, parent_key, source_key, source_pdf, qps, pool_size
            )
            row["output_pdf"] = str(output)
        target = output.with_name(self.naming.filename_for(source_pdf.stem))
        if target != output:
            if target.exists():
                raise TranslationError(
                    f"configured translation output filename already exists: {target}"
                )
            try:
                os.replace(output, target)
            except OSError as exc:
                raise TranslationError(
                    f"cannot apply configured translation filename: {output} -> {target}: {exc}"
                ) from exc
            output = target
        row["output_pdf"] = str(output)
        row["status"] = "importing"
        self.store.write(rows)
        self.attachments.import_pdf(user_id, parent_key, output)
        row["status"] = "done"
        row["last_error"] = ""
        self.store.write(rows)


def doctor(prefs_path: Path | None = None) -> dict[str, Any]:
    settings = load_pdf2zh_settings(prefs_path)
    naming = load_translation_naming()
    server = PDF2ZHClient(naming=naming)
    selected_url = server.health(settings)
    local = zotero_local.ping_status()
    local.pop("sample_item", None)
    attachments = ZoteroAttachmentClient(naming=naming)
    attachments.preflight(verify_write=False)
    webdav = {
        **attachments.configuration_status(),
        "reachable": True,
        "write_verified": False,
    }
    return {
        "ok": True,
        "pdf2zh": {**settings.summary(), "selected_server_url": selected_url},
        "zotero_local_api": local,
        "webdav": webdav,
        "naming": {
            "attachment_title": naming.attachment_title,
            "filename_template": naming.filename_template,
        },
        "queue": str(default_queue_path()),
    }


def parse_run_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD HH:MM") from exc
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError(
            "scheduled time must use local time without timezone"
        )
    local_now = datetime.now(UTC).astimezone().replace(tzinfo=None)
    if parsed <= local_now:
        raise argparse.ArgumentTypeError("scheduled time must be in the future")
    return parsed


def schedule_translation(
    run_at: datetime,
    queue: Path,
    qps: int,
    pool_size: int,
    max_items: int,
    prefs_path: Path | None = None,
) -> dict[str, Any]:
    resolved_prefs = load_pdf2zh_settings(prefs_path).prefs_path
    script = Path(__file__).resolve()
    command = [
        str(Path(sys.executable).resolve()),
        str(script),
        "run",
        "--queue",
        str(queue.expanduser().resolve()),
        "--qps",
        str(qps),
        "--pool-size",
        str(pool_size),
        "--max-items",
        str(max_items),
    ]
    command.extend(("--prefs", str(resolved_prefs.expanduser().resolve())))
    name = f"zotero-translate-{run_at:%Y%m%d-%H%M%S}"
    if zotero_runtime.is_windows():
        scheduler = shutil.which("schtasks.exe") or shutil.which("schtasks")
        if not scheduler:
            raise TranslationError("schtasks.exe is not available")
        call = [
            scheduler,
            "/Create",
            "/F",
            "/Z",
            "/SC",
            "ONCE",
            "/SD",
            run_at.strftime("%m/%d/%Y"),
            "/ST",
            run_at.strftime("%H:%M"),
            "/TN",
            name,
            "/TR",
            subprocess.list2cmdline(command),
        ]
        kind = "windows-task-scheduler"
    else:
        scheduler = shutil.which("systemd-run")
        if not scheduler:
            raise TranslationError("systemd-run is not available")
        call = [
            scheduler,
            "--user",
            "--collect",
            f"--unit={name}",
            f"--on-calendar={run_at:%Y-%m-%d %H:%M:%S}",
            *command,
        ]
        kind = "systemd-user-timer"
    result = subprocess.run(call, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise TranslationError(
            f"scheduler failed with code {result.returncode}: {detail}"
        )
    return {
        "scheduled": True,
        "scheduler": kind,
        "name": name,
        "run_at": run_at.isoformat(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("item_keys", nargs="*")
    enqueue.add_argument("--collection")
    enqueue.add_argument("--recursive", action="store_true")
    enqueue.add_argument("--limit", type=positive_int, default=1000)
    enqueue.add_argument("--queue", type=Path, default=default_queue_path())

    retry = subparsers.add_parser("retry")
    retry.add_argument("item_keys", nargs="+")
    retry.add_argument("--queue", type=Path, default=default_queue_path())

    run = subparsers.add_parser("run")
    run.add_argument("--queue", type=Path, default=default_queue_path())
    run.add_argument("--prefs", type=Path)
    run.add_argument("--qps", type=positive_int, required=True)
    run.add_argument("--pool-size", type=positive_int, required=True)
    run.add_argument("--max-items", type=positive_int, required=True)
    run.add_argument("--dry-run", action="store_true")

    check = subparsers.add_parser("doctor")
    check.add_argument("--prefs", type=Path)

    rename_watch = subparsers.add_parser("rename-watch")
    rename_watch.add_argument("--once", action="store_true")
    rename_watch.add_argument("--state", type=Path)
    service = rename_watch.add_mutually_exclusive_group()
    service.add_argument("--install-service", action="store_true")
    service.add_argument("--service-status", action="store_true")
    service.add_argument("--remove-service", action="store_true")

    schedule = subparsers.add_parser("schedule")
    schedule.add_argument("--at", type=parse_run_at, required=True)
    schedule.add_argument("--queue", type=Path, default=default_queue_path())
    schedule.add_argument("--prefs", type=Path)
    schedule.add_argument("--qps", type=positive_int, required=True)
    schedule.add_argument("--pool-size", type=positive_int, required=True)
    schedule.add_argument("--max-items", type=positive_int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "doctor":
            result = doctor(arguments.prefs)
        elif arguments.command == "rename-watch":
            if arguments.install_service:
                result = configure_rename_watch_service("install")
            elif arguments.service_status:
                result = configure_rename_watch_service("status")
            elif arguments.remove_service:
                result = configure_rename_watch_service("remove")
            else:
                result = watch_manual_translation_renames(
                    once=arguments.once,
                    state_path=arguments.state,
                )
        elif arguments.command == "schedule":
            result = schedule_translation(
                arguments.at,
                arguments.queue,
                arguments.qps,
                arguments.pool_size,
                arguments.max_items,
                arguments.prefs,
            )
        else:
            worker = TranslationWorker(
                QueueStore(arguments.queue),
                prefs_path=getattr(arguments, "prefs", None),
            )
            if arguments.command == "enqueue":
                if bool(arguments.item_keys) == bool(arguments.collection):
                    raise TranslationError(
                        "provide item keys or one --collection, but not both"
                    )
                result = (
                    worker.enqueue(arguments.item_keys)
                    if arguments.item_keys
                    else worker.enqueue_collection(
                        arguments.collection, arguments.recursive, arguments.limit
                    )
                )
            elif arguments.command == "retry":
                result = worker.retry(arguments.item_keys)
            else:
                result = worker.run_batch(
                    arguments.qps,
                    arguments.pool_size,
                    arguments.max_items,
                    arguments.dry_run,
                )
    except (
        TranslationError,
        zotero_runtime.RuntimeConfigError,
        zotero_web_api.ZoteroWriteError,
        requests.RequestException,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
