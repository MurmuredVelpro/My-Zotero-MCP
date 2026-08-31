#!/usr/bin/env python3
"""Client helpers for MinerU's cloud precision API."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import requests
from mineru.exceptions import raise_for_code

from . import zotero_http, zotero_runtime

API_BASE = "https://mineru.net/api/v4"
SDK_SOURCE = "zotero-mcp-local"
DEFAULT_TOKEN_FILE = "mineru_api_token.secret"
MODEL_VERSIONS = {"pipeline", "vlm"}
MAX_BATCH_FILES = 50
TRANSFER_TIMEOUT = (10, 300)
API_TIMEOUT = (30, 120)
REQUIRED_RESULT_ARTIFACTS = (
    "result.zip",
    "full.md",
    "content_list.json",
    "content_list_v2.json",
    "layout.json",
    "model.json",
    "origin.pdf",
)


class MinerUError(RuntimeError):
    """Raised when MinerU rejects a request or returns an invalid response."""


class ApiClient:
    """MinerU API transport routed through Zotero MCP's NORMAL profile."""

    def __init__(self, token: str, base_url: str, source: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.source = source
        self.session = zotero_http.routed_session(zotero_http.RouteType.NORMAL)
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def close(self) -> None:
        self.session.close()

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        headers = {"source": self.source} if self.source else None
        response = zotero_http.session_request(
            self.session,
            "POST",
            f"{self.base_url}/{path.lstrip('/')}",
            route=zotero_http.RouteType.NORMAL,
            json=json,
            headers=headers,
            timeout=API_TIMEOUT,
        )
        return self._handle(response)

    def get(self, path: str) -> dict[str, Any]:
        response = zotero_http.session_request(
            self.session,
            "GET",
            f"{self.base_url}/{path.lstrip('/')}",
            route=zotero_http.RouteType.NORMAL,
            timeout=API_TIMEOUT,
        )
        return self._handle(response)

    @staticmethod
    def _handle(response: requests.Response) -> dict[str, Any]:
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise MinerUError("MinerU API returned a non-object response")
        code = body.get("code", 0)
        if code != 0:
            raise_for_code(
                code,
                str(body.get("msg", "unknown error")),
                str(body.get("trace_id", "")),
            )
        return body


def output_root() -> Path:
    configured = zotero_runtime.configured_path(
        "ZOTERO_MINERU_DIR", "mineru", "output_dir"
    )
    return configured or Path.home() / "tools" / "Zotero_MinerU"


DEFAULT_OUTPUT_ROOT = output_root()


def default_token_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "mineru" / DEFAULT_TOKEN_FILE


def token_path() -> Path:
    configured = zotero_runtime.configured_path(
        "MINERU_API_TOKEN_FILE", "mineru", "token_file"
    )
    if configured:
        return configured
    return default_token_path()


def load_token(path: Path | None = None) -> str:
    token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if token:
        return token
    path = token_path() if path is None else path
    if not path.is_file():
        raise MinerUError(f"MinerU Token file not found: {path}")
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise MinerUError(f"MinerU Token file permissions must be 600: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise MinerUError(f"MinerU Token file is empty: {path}")
    return token


@contextmanager
def api_client(token: str | None = None):
    # The public SDK omits split-upload primitives. Pin its version and isolate
    # the official low-level client here to preserve recoverable batch uploads.
    client = ApiClient(token or load_token(), API_BASE, source=SDK_SOURCE)
    try:
        yield client
    finally:
        client.close()


def response_data(payload: dict[str, Any], action: str) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MinerUError(f"{action} returned no data object")
    return data


def request_upload_batch(
    file_specs: list[dict[str, Any]],
    *,
    model_version: str = "vlm",
    language: str = "en",
    enable_formula: bool = True,
    enable_table: bool = True,
    token: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(file_specs) <= MAX_BATCH_FILES:
        raise MinerUError(f"MinerU batch must contain 1-{MAX_BATCH_FILES} files")
    if model_version not in MODEL_VERSIONS:
        raise MinerUError(
            f"Unsupported model_version={model_version!r}; use pipeline or vlm"
        )
    normalized_specs: list[dict[str, Any]] = []
    for file_spec in file_specs:
        if not isinstance(file_spec, dict):
            raise MinerUError("MinerU file specification must be an object")
        name = str(file_spec.get("name", "")).strip()
        if not name:
            raise MinerUError("MinerU file specification requires a name")
        normalized = {"name": name}
        for field in ("data_id", "is_ocr", "page_ranges"):
            if field in file_spec and file_spec[field] is not None:
                normalized[field] = file_spec[field]
        normalized_specs.append(normalized)
    body = {
        "files": normalized_specs,
        "model_version": model_version,
        "language": language,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
    }
    action = "Request MinerU upload URL"
    try:
        with api_client(token) as client:
            payload = client.post("/file-urls/batch", json=body)
    except Exception as exc:
        raise MinerUError(f"{action} failed: {exc}") from exc
    data = response_data(payload, action)
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls")
    if (
        not batch_id
        or not isinstance(file_urls, list)
        or len(file_urls) != len(normalized_specs)
        or any(
            not isinstance(url, str) or not url.startswith("https://")
            for url in file_urls
        )
    ):
        raise MinerUError("MinerU returned an invalid batch_id or upload URL list")
    return {"batch_id": batch_id, "file_urls": file_urls}


def upload_file(file_path: Path, upload_url: str) -> int:
    file_path = file_path.expanduser().resolve()
    if not file_path.is_file():
        raise MinerUError(f"Input file not found: {file_path}")
    if not upload_url.startswith("https://"):
        raise MinerUError("MinerU upload URL is not HTTPS")

    try:
        with file_path.open("rb") as handle:
            response = zotero_http.put(
                upload_url,
                route=zotero_http.RouteType.NORMAL,
                data=handle,
                timeout=TRANSFER_TIMEOUT,
            )
            response.raise_for_status()
    except Exception as exc:
        raise MinerUError(f"Upload PDF to MinerU failed: {file_path}: {exc}") from exc
    return response.status_code


def submit_file(
    file_path: Path,
    *,
    data_id: str,
    model_version: str = "vlm",
    language: str = "en",
    enable_formula: bool = True,
    enable_table: bool = True,
    is_ocr: bool = False,
    page_ranges: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    file_path = file_path.expanduser().resolve()
    if not file_path.is_file():
        raise MinerUError(f"Input file not found: {file_path}")
    file_spec: dict[str, Any] = {
        "name": file_path.name,
        "data_id": data_id,
        "is_ocr": is_ocr,
    }
    if page_ranges:
        file_spec["page_ranges"] = page_ranges
    batch = request_upload_batch(
        [file_spec],
        model_version=model_version,
        language=language,
        enable_formula=enable_formula,
        enable_table=enable_table,
        token=token,
    )
    upload_file(file_path, batch["file_urls"][0])
    return {
        "batch_id": batch["batch_id"],
        "data_id": data_id,
        "file_name": file_path.name,
        "model_version": model_version,
    }


def get_batch(batch_id: str, *, token: str | None = None) -> dict[str, Any]:
    if not batch_id.strip():
        raise MinerUError("batch_id is required")
    action = "Query MinerU batch"
    try:
        with api_client(token) as client:
            payload = client.get(f"/extract-results/batch/{batch_id}")
    except Exception as exc:
        raise MinerUError(f"{action} failed: {exc}") from exc
    return response_data(payload, action)


def extract_results(batch: dict[str, Any]) -> list[dict[str, Any]]:
    results = batch.get("extract_result")
    if not isinstance(results, list) or not results:
        raise MinerUError("MinerU batch contains no extract_result")
    if any(not isinstance(result, dict) for result in results):
        raise MinerUError("MinerU extract_result is invalid")
    return results


def first_extract_result(batch: dict[str, Any]) -> dict[str, Any]:
    return extract_results(batch)[0]


def item_directory_name(item: dict[str, Any]) -> str:
    data = item.get("data", {})
    key = str(data.get("key") or item.get("key", "")).strip()
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", key).strip("_")
    if not key:
        raise MinerUError("Zotero item has no usable key")
    return key


def default_result_dir(item: dict[str, Any]) -> Path:
    return DEFAULT_OUTPUT_ROOT / item_directory_name(item)


def safe_extract(zip_path: Path, output_dir: Path) -> None:
    root = output_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (root / member.filename).resolve()
            if target != root and root not in target.parents:
                raise MinerUError(f"Unsafe path in MinerU ZIP: {member.filename}")
        archive.extractall(root)


def canonicalize_artifacts(output_dir: Path) -> None:
    output_dir = output_dir.expanduser().resolve()
    paths = list(output_dir.rglob("*"))
    files = [path for path in paths if path.is_file()]
    directories = [path for path in paths if path.is_dir()]
    artifacts = (
        ("full.md", None),
        ("content_list.json", "_content_list.json"),
        ("content_list_v2.json", "_content_list_v2.json"),
        ("model.json", "_model.json"),
        ("layout.json", None),
        ("origin.pdf", "_origin.pdf"),
    )
    for target_name, suffix in artifacts:
        matches = sorted(
            path
            for path in files
            if path.exists()
            and (path.name == target_name or (suffix and path.name.endswith(suffix)))
        )
        if not matches:
            continue
        if len(matches) > 1:
            raise MinerUError(
                f"Multiple MinerU artifacts match {target_name}: "
                + ", ".join(str(path) for path in matches)
            )
        source = matches[0]
        target = output_dir / target_name
        if source == target:
            continue
        if target.exists():
            raise MinerUError(f"MinerU artifact already exists: {target}")
        source.replace(target)

    image_dirs = sorted(
        path for path in directories if path.exists() and path.name == "images"
    )
    if len(image_dirs) > 1:
        raise MinerUError(
            "Multiple MinerU image directories found: "
            + ", ".join(str(path) for path in image_dirs)
        )
    if image_dirs:
        source = image_dirs[0]
        target = output_dir / "images"
        if source != target:
            if target.exists():
                raise MinerUError(f"MinerU image directory already exists: {target}")
            source.replace(target)

    directories = sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def artifact_summary(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    files = sorted(path for path in output_dir.rglob("*") if path.is_file())

    def find(*names: str, suffix: str | None = None) -> str | None:
        return next(
            (
                str(path)
                for path in files
                if path.name in names or (suffix and path.name.endswith(suffix))
            ),
            None,
        )

    return {
        "output_dir": str(output_dir),
        "zip": find("result.zip"),
        "full_md": find("full.md"),
        "content_list_json": find("content_list.json", suffix="_content_list.json"),
        "content_list_v2_json": find(
            "content_list_v2.json", suffix="_content_list_v2.json"
        ),
        "middle_json": find("middle.json", "layout.json"),
        "model_json": find("model.json", suffix="_model.json"),
        "original_file": find("origin.pdf", suffix="_origin.pdf"),
        "file_count": len(files),
    }


def missing_result_artifacts(output_dir: Path) -> list[str]:
    return [
        name
        for name in REQUIRED_RESULT_ARTIFACTS
        if not (output_dir / name).is_file() or (output_dir / name).stat().st_size == 0
    ]


def install_result_directory(
    staged_dir: Path,
    output_dir: Path,
    recovery_dir: Path | None = None,
) -> None:
    recovery_dir = recovery_dir or output_dir.with_name(
        f".{output_dir.name}.previous-{uuid.uuid4().hex}"
    )
    replaced_existing = output_dir.exists()
    if replaced_existing:
        if not output_dir.is_dir():
            raise MinerUError(f"MinerU output path is not a directory: {output_dir}")
        if recovery_dir.exists():
            raise MinerUError(
                f"Replacement recovery directory already exists: {recovery_dir}"
            )
        output_dir.replace(recovery_dir)
    try:
        staged_dir.replace(output_dir)
    except Exception:
        if replaced_existing:
            try:
                recovery_dir.replace(output_dir)
            except Exception as restore_error:
                raise MinerUError(
                    "Failed to install MinerU result and restore previous output; "
                    f"recovery remains at {recovery_dir}"
                ) from restore_error
        raise
    if replaced_existing:
        shutil.rmtree(recovery_dir)


def find_local_result(item_key: str) -> dict[str, Any] | None:
    item_dir = DEFAULT_OUTPUT_ROOT / item_directory_name({"data": {"key": item_key}})
    if missing_result_artifacts(item_dir):
        return None
    full_md = item_dir / "full.md"
    return {"output_dir": str(item_dir.resolve()), "full_md": str(full_md.resolve())}


def download_and_extract(full_zip_url: str, output_dir: Path) -> dict[str, Any]:
    if not full_zip_url.startswith("https://"):
        raise MinerUError("MinerU result URL is not HTTPS")
    output_dir = output_dir.expanduser().resolve()
    if output_dir.is_dir():
        existing = artifact_summary(output_dir)
        if not missing_result_artifacts(output_dir):
            return existing
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staged_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.download-",
            dir=output_dir.parent,
        )
    )
    try:
        zip_path = staged_dir / "result.zip"
        partial = staged_dir / "result.zip.part"
        try:
            with zotero_http.get(
                full_zip_url,
                route=zotero_http.RouteType.NORMAL,
                stream=True,
                timeout=TRANSFER_TIMEOUT,
            ) as response:
                response.raise_for_status()
                expected = response.headers.get("Content-Length")
                expected_bytes = int(expected) if expected else None
                downloaded = 0
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if downloaded == 0:
                    raise MinerUError("MinerU result download was empty")
                if expected_bytes is not None and downloaded != expected_bytes:
                    raise MinerUError(
                        "MinerU result download was incomplete: "
                        f"expected={expected_bytes} received={downloaded}"
                    )
        except Exception as exc:
            if isinstance(exc, MinerUError):
                raise
            raise MinerUError(f"Download MinerU result failed: {exc}") from exc
        partial.replace(zip_path)
        safe_extract(zip_path, staged_dir)
        canonicalize_artifacts(staged_dir)
        missing = missing_result_artifacts(staged_dir)
        if missing:
            raise MinerUError(
                f"Downloaded MinerU result is incomplete: missing {missing}"
            )
        install_result_directory(staged_dir, output_dir)
        return artifact_summary(output_dir)
    finally:
        shutil.rmtree(staged_dir, ignore_errors=True)
