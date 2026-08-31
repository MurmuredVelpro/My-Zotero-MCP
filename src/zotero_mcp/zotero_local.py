#!/usr/bin/env python3
"""Read-only helper for a local Zotero API from Windows or WSL.

The script does not modify Zotero. It queries metadata through the Local API and
resolves PDF attachment paths under the configured Zotero storage folder.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from . import zotero_http, zotero_runtime

LOCAL_API_PORT = "23119"
WSL_PROXY_PORT = "23120"
WINDOWS_DEFAULT_API = f"http://127.0.0.1:{LOCAL_API_PORT}/api"
PDF_TOOL_ENVS = {"pdftotext": "PDFTOTEXT", "pdftoppm": "PDFTOPPM"}
ENGLISH_PDF_MAX_CJK_SHARE = 0.20
TRANSLATION_ATTACHMENT_TITLES = {"cn", "zh", "zh-cn", "chinese"}
TRANSLATION_FILENAME_MARKERS = ("全文翻译", "机器翻译", "中文翻译", "中译", "译文")
_SELECTED_API_BASE: str | None = None


def is_windows() -> bool:
    return zotero_runtime.is_windows()


def default_text_out_dir() -> Path:
    return Path(tempfile.gettempdir()) / "zotero_texts"


def default_api_base() -> str:
    return local_api_candidates()[0]


def configured_local_api() -> str | None:
    override = os.environ.get("ZOTERO_LOCAL_API", "").strip()
    if override:
        return override.rstrip("/")
    configured = zotero_runtime.config_string("zotero", "local_api")
    return configured.rstrip("/") if configured else None


def local_api_candidates() -> list[str]:
    configured = configured_local_api()
    if configured:
        return [configured]
    if is_windows():
        return [WINDOWS_DEFAULT_API]

    candidates = [WINDOWS_DEFAULT_API]
    gateway = zotero_http.wsl_gateway_ip()
    if gateway:
        candidates.extend(
            [
                f"http://{gateway}:{LOCAL_API_PORT}/api",
                f"http://{gateway}:{WSL_PROXY_PORT}/api",
            ]
        )
    return list(dict.fromkeys(candidates))


def api_base() -> str:
    configured = configured_local_api()
    if configured:
        return configured
    return _SELECTED_API_BASE or default_api_base()


def windows_storage_candidates() -> list[Path]:
    profiles = [Path.home()]
    userprofile = zotero_runtime.windows_user_profile()
    if userprofile:
        profiles.insert(0, Path(userprofile))
    candidates = [profile / "Zotero" / "storage" for profile in profiles]
    candidates.extend(
        profile / "Documents" / "Zotero" / "storage" for profile in profiles
    )
    return list(dict.fromkeys(candidates))


def wsl_storage_candidates() -> list[Path]:
    candidates: list[Path] = []
    userprofile = zotero_runtime.windows_user_profile()
    if userprofile:
        converted = windows_path_to_wsl_path(userprofile)
        if converted:
            candidates.append(converted / "Zotero" / "storage")
            candidates.append(converted / "Documents" / "Zotero" / "storage")
    if not candidates:
        candidates.append(
            Path("/mnt/c/Users") / Path.home().name / "Zotero" / "storage"
        )
    return list(dict.fromkeys(candidates))


def windows_path_to_wsl_path(value: str) -> Path | None:
    return zotero_runtime.windows_path_to_wsl_path(value)


def first_existing_path(candidates: list[Path]) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def storage_root() -> Path:
    configured = zotero_runtime.configured_path("ZOTERO_STORAGE", "zotero", "storage")
    if configured:
        return configured
    if is_windows():
        return first_existing_path(windows_storage_candidates())
    return first_existing_path(wsl_storage_candidates())


def active_env_prefix() -> Path:
    conda_prefix = os.environ.get("CONDA_PREFIX")
    sys_prefix = Path(sys.prefix)
    if not conda_prefix:
        return sys_prefix

    conda_path = Path(conda_prefix)
    sys_prefix_text = str(sys_prefix).replace("/", "\\").casefold()
    if is_windows() and sys_prefix != conda_path and "\\envs\\" in sys_prefix_text:
        return sys_prefix
    if not is_windows() and sys_prefix != conda_path and "envs" in sys_prefix.parts:
        return sys_prefix
    return conda_path


def candidate_pdf_tool_paths(tool: str) -> list[Path]:
    executable = f"{tool}.exe" if is_windows() else tool
    prefix = active_env_prefix()
    if is_windows():
        return [
            prefix / "Library" / "bin" / executable,
            prefix / "Scripts" / executable,
        ]
    return [prefix / "bin" / executable]


def resolve_pdf_tool(tool: str) -> Path | None:
    env_name = PDF_TOOL_ENVS.get(tool, tool.upper())
    override = os.environ.get(env_name)
    if override:
        return Path(override)
    found = shutil.which(tool)
    if found:
        return Path(found)
    for candidate in candidate_pdf_tool_paths(tool):
        if candidate.exists():
            return candidate
    return None


def pdf_tool_command(tool: str) -> str:
    resolved = resolve_pdf_tool(tool)
    if resolved:
        return str(resolved)
    env_name = PDF_TOOL_ENVS.get(tool, tool.upper())
    raise SystemExit(
        f"Required command not found: {tool}. Install poppler in the active Python/conda "
        f"environment or set {env_name} to the executable path."
    )


def zotero_get(path: str, params: dict[str, Any] | None = None) -> Any:
    global _SELECTED_API_BASE

    candidates = local_api_candidates()
    if _SELECTED_API_BASE in candidates:
        candidates = [
            _SELECTED_API_BASE,
            *[base for base in candidates if base != _SELECTED_API_BASE],
        ]

    failures: list[str] = []
    for base in candidates:
        url = f"{base}/{path.lstrip('/')}"
        try:
            response = zotero_http.get(
                url,
                route=zotero_http.RouteType.LOCAL,
                params=params,
                timeout=10,
                headers={
                    "Host": "localhost:23119",
                    "Zotero-Allowed-Request": "1",
                    "Zotero-API-Version": "3",
                },
            )
        except requests.RequestException as exc:
            failures.append(f"{base}: {type(exc).__name__}: {exc}")
            continue

        response.raise_for_status()
        _SELECTED_API_BASE = base
        if not response.text.strip():
            return None
        return response.json()

    tried = ", ".join(candidates)
    detail = failures[-1] if failures else "no candidate was attempted"
    raise requests.ConnectionError(
        f"Unable to reach Zotero Local API. Tried: {tried}. Last error: {detail}"
    )


def fetch_paginated(
    path: str,
    params: dict[str, Any] | None = None,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    if max_items is not None and max_items < 1:
        return []

    items: list[dict[str, Any]] = []
    page_size = 100
    while max_items is None or len(items) < max_items:
        limit = (
            page_size if max_items is None else min(page_size, max_items - len(items))
        )
        page_params = dict(params or {})
        page_params.update({"start": len(items), "limit": limit})
        page = zotero_get(path, page_params) or []
        if not page:
            break
        items.extend(page)
        if len(page) < limit:
            break
    return items


class _NoteTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "div", "li", "p"} and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"div", "li", "p"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def note_html_to_text(note_html: str) -> str:
    parser = _NoteTextParser()
    parser.feed(note_html)
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def item_annotations(item_key: str, limit: int = 100) -> list[dict[str, Any]]:
    item = zotero_get(f"users/0/items/{quote(item_key)}")
    data = item.get("data", {})
    item_type = data.get("itemType")
    if item_type == "attachment":
        parent_item_key = str(data.get("parentItem") or item_key)
        attachments = [item]
    else:
        parent_item_key = str(data.get("key") or item.get("key") or item_key)
        attachments = [
            child
            for child in fetch_paginated(
                f"users/0/items/{quote(parent_item_key)}/children",
                max_items=1000,
            )
            if child.get("data", {}).get("itemType") == "attachment"
        ]

    records: list[dict[str, Any]] = []
    for attachment in attachments:
        if len(records) >= limit:
            break
        attachment_data = attachment.get("data", {})
        attachment_key = str(attachment_data.get("key") or attachment.get("key") or "")
        annotations = fetch_paginated(
            f"users/0/items/{quote(attachment_key)}/children",
            {"itemType": "annotation", "sort": "dateAdded", "direction": "asc"},
            max_items=limit - len(records),
        )
        records.extend(
            _annotation_record(
                annotation,
                parent_item_key=parent_item_key,
                attachment_key=attachment_key,
                attachment_title=str(attachment_data.get("title") or ""),
            )
            for annotation in annotations
        )
    return records


def _annotation_record(
    annotation: dict[str, Any],
    *,
    parent_item_key: str,
    attachment_key: str,
    attachment_title: str,
) -> dict[str, Any]:
    data = annotation.get("data", {})
    position = data.get("annotationPosition")
    if isinstance(position, str):
        try:
            position = json.loads(position)
        except json.JSONDecodeError:
            position = None
    page_index = position.get("pageIndex") if isinstance(position, dict) else None
    return {
        "annotation_key": str(data.get("key") or annotation.get("key") or ""),
        "item_key": parent_item_key,
        "attachment_key": attachment_key,
        "attachment_title": attachment_title,
        "type": data.get("annotationType"),
        "page": data.get("annotationPageLabel"),
        "page_index": page_index,
        "text": data.get("annotationText") or "",
        "comment": data.get("annotationComment") or "",
        "color": data.get("annotationColor"),
        "position": position,
        "tags": [
            tag.get("tag") if isinstance(tag, dict) else str(tag)
            for tag in data.get("tags", [])
        ],
        "date_added": data.get("dateAdded"),
        "date_modified": data.get("dateModified"),
    }


def item_notes(item_key: str, limit: int = 20) -> list[dict[str, Any]]:
    item = zotero_get(f"users/0/items/{quote(item_key)}")
    data = item.get("data", {})
    if data.get("itemType") == "note":
        parent_item_key = str(data.get("parentItem") or "")
        notes = [item]
    else:
        parent_item_key = str(data.get("key") or item.get("key") or item_key)
        notes = fetch_paginated(
            f"users/0/items/{quote(parent_item_key)}/children",
            {"itemType": "note", "sort": "dateAdded", "direction": "asc"},
            max_items=limit,
        )

    return [
        {
            "note_key": str(note.get("data", {}).get("key") or note.get("key") or ""),
            "item_key": parent_item_key,
            "note_html": str(note.get("data", {}).get("note") or ""),
            "note_text": note_html_to_text(str(note.get("data", {}).get("note") or "")),
            "tags": [
                tag.get("tag") if isinstance(tag, dict) else str(tag)
                for tag in note.get("data", {}).get("tags", [])
            ],
            "date_added": note.get("data", {}).get("dateAdded"),
            "date_modified": note.get("data", {}).get("dateModified"),
        }
        for note in notes[:limit]
    ]


def fetch_all_collections() -> list[dict[str, Any]]:
    return fetch_paginated("users/0/collections")


def collection_index(collections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(collection.get("data", {}).get("key") or collection.get("key")): collection
        for collection in collections
        if collection.get("data", {}).get("key") or collection.get("key")
    }


def collection_path(key: str, collections_by_key: dict[str, dict[str, Any]]) -> str:
    names: list[str] = []
    current_key = key
    seen: set[str] = set()
    while current_key and current_key not in seen:
        seen.add(current_key)
        collection = collections_by_key.get(current_key)
        if not collection:
            names.append(current_key)
            break
        data = collection.get("data", {})
        names.append(data.get("name") or current_key)
        current_key = data.get("parentCollection") or ""
    return " > ".join(reversed(names))


def collection_summary(
    collection: dict[str, Any], collections_by_key: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    data = collection.get("data", {})
    meta = collection.get("meta", {})
    key = str(data.get("key") or collection.get("key") or "")
    return {
        "key": key,
        "name": data.get("name", ""),
        "path": collection_path(key, collections_by_key),
        "parent_key": data.get("parentCollection") or None,
        "num_items": meta.get("numItems"),
        "num_collections": meta.get("numCollections"),
    }


def collection_summaries(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collections_by_key = collection_index(collections)
    summaries = [
        collection_summary(collection, collections_by_key) for collection in collections
    ]
    return sorted(summaries, key=lambda summary: summary["path"].casefold())


def descendant_collection_keys(
    root_key: str, collections_by_key: dict[str, dict[str, Any]]
) -> list[str]:
    children_by_parent: dict[str, list[str]] = {}
    for child_key, collection in collections_by_key.items():
        parent_key = collection.get("data", {}).get("parentCollection") or ""
        children_by_parent.setdefault(str(parent_key), []).append(child_key)
    for child_keys in children_by_parent.values():
        child_keys.sort(
            key=lambda child_key: collection_path(
                child_key, collections_by_key
            ).casefold()
        )

    ordered: list[str] = []
    queue = deque([root_key])
    seen: set[str] = set()
    while queue:
        key = queue.popleft()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
        queue.extend(children_by_parent.get(key, ()))
    return ordered


def resolve_top_level_item(item: dict[str, Any]) -> dict[str, Any]:
    current = item
    seen: set[str] = set()
    while True:
        parent_key = current.get("data", {}).get("parentItem")
        if not parent_key or parent_key in seen:
            return current
        seen.add(parent_key)
        current = zotero_get(f"users/0/items/{quote(parent_key)}")


def item_collection_summaries(
    item: dict[str, Any], collections_by_key: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for key in item.get("data", {}).get("collections", []):
        collection = collections_by_key.get(key)
        if collection:
            summaries.append(collection_summary(collection, collections_by_key))
        else:
            summaries.append(
                {
                    "key": key,
                    "name": "",
                    "path": key,
                    "parent_key": None,
                    "num_items": None,
                    "num_collections": None,
                }
            )
    return sorted(summaries, key=lambda summary: summary["path"].casefold())


def fetch_collection_items(collection_key: str, max_items: int) -> list[dict[str, Any]]:
    return fetch_paginated(
        f"users/0/collections/{quote(collection_key)}/items/top",
        {"itemType": "-attachment", "sort": "title", "direction": "asc"},
        max_items=max_items,
    )


def collect_collection_items(
    root_key: str,
    collections_by_key: dict[str, dict[str, Any]],
    recursive: bool,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    target_keys = (
        descendant_collection_keys(root_key, collections_by_key)
        if recursive
        else [root_key]
    )
    items_by_key: dict[str, dict[str, Any]] = {}
    memberships: dict[str, list[str]] = {}
    for collection_key in target_keys:
        for item in fetch_collection_items(collection_key, limit):
            data = item.get("data", {})
            if data.get("parentItem") or data.get("itemType") in {
                "attachment",
                "note",
                "annotation",
            }:
                continue
            item_key = str(data.get("key") or item.get("key") or "")
            if not item_key:
                continue
            if item_key not in items_by_key and len(items_by_key) >= limit:
                continue
            items_by_key.setdefault(item_key, item)
            memberships.setdefault(item_key, []).append(collection_key)

    items = sorted(
        items_by_key.values(),
        key=lambda item: item.get("data", {}).get("title", "").casefold(),
    )
    return items, memberships


def creator_summary(item: dict[str, Any]) -> str:
    meta = item.get("meta", {})
    if meta.get("creatorSummary"):
        return meta["creatorSummary"]
    creators = item.get("data", {}).get("creators", [])
    if not creators:
        return ""
    first = creators[0]
    return first.get("lastName") or first.get("name") or ""


def attachment_key_from_links(item: dict[str, Any]) -> str | None:
    attachment = item.get("links", {}).get("attachment")
    if not attachment:
        return None
    href = attachment.get("href", "").rstrip("/")
    if not href:
        return None
    return href.split("/")[-1]


def find_pdf_for_attachment(attachment_key: str) -> list[Path]:
    attachment_dir = storage_root() / attachment_key
    if not attachment_dir.exists():
        return []
    return sorted(attachment_dir.glob("*.pdf"))


def first_pdf_for_item(item: dict[str, Any]) -> Path:
    attachment_key = attachment_key_from_links(item)
    if not attachment_key:
        raise SystemExit("No PDF attachment link found for this item.")
    pdfs = find_pdf_for_attachment(attachment_key)
    if not pdfs:
        raise SystemExit(f"No PDF found under {storage_root() / attachment_key}")
    return pdfs[0]


def pdf_attachments_for_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    data = item.get("data", {})
    item_key = data.get("key") or item.get("key")
    if not item_key:
        raise SystemExit("Zotero item has no key.")
    primary_key = attachment_key_from_links(item)
    children = zotero_get(f"users/0/items/{quote(str(item_key))}/children") or []
    attachments = []
    for child in children:
        child_data = child.get("data", {})
        attachment_key = child_data.get("key") or child.get("key")
        filename = str(child_data.get("filename") or "")
        content_type = str(child_data.get("contentType") or "")
        if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        pdfs = find_pdf_for_attachment(str(attachment_key))
        if not pdfs:
            continue
        attachments.append(
            {
                "key": str(attachment_key),
                "title": str(child_data.get("title") or ""),
                "filename": filename or pdfs[0].name,
                "path": pdfs[0],
                "primary": attachment_key == primary_key,
            }
        )
    return attachments


def has_translation_marker(title: str, filename: str) -> bool:
    if title.strip().casefold() in TRANSLATION_ATTACHMENT_TITLES:
        return True
    stem = Path(filename).stem.casefold()
    if any(marker in stem for marker in TRANSLATION_FILENAME_MARKERS):
        return True
    return bool(re.search(r"(?:^|[_\-\s(\[])(?:cn|zh|zh-cn)(?:$|[_\-\s)\]])", stem))


def pdf_text_language_stats(pdf: Path) -> tuple[float, int]:
    text = run_capture(
        [pdf_tool_command("pdftotext"), "-f", "1", "-l", "3", str(pdf), "-"]
    )
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    letters = cjk + latin
    return cjk / max(letters, 1), letters


def english_pdf_attachment_for_item(item: dict[str, Any]) -> dict[str, Any]:
    attachments = pdf_attachments_for_item(item)
    if not attachments:
        raise SystemExit("No local PDF attachments found for this item.")

    eligible = []
    for attachment in attachments:
        if has_translation_marker(attachment["title"], attachment["filename"]):
            continue
        cjk_share, letters = pdf_text_language_stats(attachment["path"])
        if letters < 200 or cjk_share >= ENGLISH_PDF_MAX_CJK_SHARE:
            continue
        attachment["cjk_share"] = cjk_share
        eligible.append(attachment)

    if not eligible:
        raise SystemExit("No confidently English PDF attachment found for this item.")
    eligible.sort(
        key=lambda attachment: (
            not attachment["primary"],
            attachment["cjk_share"],
            attachment["filename"].casefold(),
        )
    )
    return eligible[0]


def english_pdf_for_item(item: dict[str, Any]) -> Path:
    return english_pdf_attachment_for_item(item)["path"]


def output_stem(item: dict[str, Any]) -> str:
    data = item.get("data", {})
    key = data.get("key") or item.get("key", "zotero")
    date = data.get("date", "")
    year_match = re.search(r"\d{4}", date)
    year = year_match.group(0) if year_match else "unknown"
    authors = creator_summary(item).replace(" 等", "").replace(" et al.", "")
    authors = re.sub(r"[^A-Za-z0-9_-]+", "_", authors).strip("_") or "zotero"
    return f"{authors.lower()}_{year}_{key.lower()}"


def default_text_filename(item: dict[str, Any]) -> str:
    title = item.get("data", {}).get("title", "")
    title = re.sub(r"\s+", " ", title).strip()
    title = title.encode("ascii", "ignore").decode("ascii")
    title = re.sub(r'[\/\\:*?"<>|]', "_", title)
    title = re.sub(r"\s+", "_", title)
    title = re.sub(r"_+", "_", title)
    title = title[:20].strip(" ._") or "untitled"
    return f"{title}.txt"


def ensure_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_text_output_path(
    output: str | None, out_dir: str | None, item: dict[str, Any]
) -> Path:
    base_dir = Path(out_dir) if out_dir else default_text_out_dir()
    if output:
        out_file = Path(output)
        if out_file.is_absolute():
            ensure_output_dir(out_file.parent)
            return out_file
        out_dir_path = ensure_output_dir(base_dir)
        resolved = out_dir_path / out_file
        ensure_output_dir(resolved.parent)
        return resolved
    out_dir_path = ensure_output_dir(base_dir)
    return out_dir_path / default_text_filename(item)


def run_command(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Command failed with exit code {exc.returncode}: {' '.join(command)}"
        ) from exc


def run_capture(command: list[str]) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise SystemExit(
            f"Command failed with exit code {exc.returncode}: {' '.join(command)}\n{stderr}"
        ) from exc
    return result.stdout


def normalize_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"https?://doi\.org/", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def score_item(item: dict[str, Any], query: str, field: str) -> float:
    data = item.get("data", {})
    if field == "doi":
        doi = normalize_text(data.get("DOI", ""))
        query_norm = normalize_text(query)
        if not doi:
            return 0.0
        return (
            1.0
            if doi == query_norm
            else difflib.SequenceMatcher(None, doi, query_norm).ratio()
        )
    title = normalize_text(data.get("title", ""))
    query_norm = normalize_text(query)
    if not title:
        return 0.0
    if title == query_norm:
        return 1.0
    if query_norm in title or title in query_norm:
        return 0.95
    return difflib.SequenceMatcher(None, title, query_norm).ratio()


def fetch_items_page(start: int, limit: int = 100) -> list[dict[str, Any]]:
    return (
        zotero_get(
            "users/0/items",
            {
                "start": start,
                "limit": limit,
                "sort": "dateModified",
                "direction": "desc",
                "itemType": "-attachment",
            },
        )
        or []
    )


def fetch_items_for_local_match(max_items: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_size = 100
    for start in range(0, max_items, page_size):
        page = fetch_items_page(start, min(page_size, max_items - start))
        if not page:
            break
        items.extend(page)
        if len(page) < page_size:
            break
    return items


def format_item_summary(summary: dict[str, Any]) -> str:
    lines = [f"- key: {summary['key']}"]
    if summary["title"]:
        lines.append(f"  title: {summary['title']}")
    if summary["authors"]:
        lines.append(f"  authors: {summary['authors']}")
    if summary["date"]:
        lines.append(f"  date: {summary['date']}")
    if summary["DOI"]:
        lines.append(f"  DOI: {summary['DOI']}")
    if summary["url"]:
        lines.append(f"  url: {summary['url']}")

    attachment_key = summary["attachment_key"]
    if attachment_key:
        lines.append(f"  attachment_key: {attachment_key}")
        pdfs = summary["pdfs"]
        if pdfs:
            for pdf in pdfs:
                lines.append(f"  pdf: {pdf}")
        else:
            lines.append(f"  pdf: not found under {storage_root() / attachment_key}")
    return "\n".join(lines)


def format_item_summaries(summaries: list[dict[str, Any]], empty_message: str) -> str:
    if not summaries:
        return empty_message
    return "\n".join(format_item_summary(summary) for summary in summaries)


def format_item(item: dict[str, Any]) -> str:
    return format_item_summary(item_summary(item))


def print_item(item: dict[str, Any], show_json: bool = False) -> None:
    if show_json:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return
    print(format_item(item))


def item_summary(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {})
    key = data.get("key") or item.get("key", "")
    attachment_key = attachment_key_from_links(item)
    pdfs = find_pdf_for_attachment(attachment_key) if attachment_key else []
    return {
        "key": key,
        "itemType": data.get("itemType", ""),
        "title": data.get("title", ""),
        "authors": creator_summary(item),
        "date": data.get("date", "") or item.get("meta", {}).get("parsedDate", ""),
        "DOI": data.get("DOI", ""),
        "url": data.get("url", ""),
        "abstractNote": data.get("abstractNote", ""),
        "extra": data.get("extra", ""),
        "publicationTitle": data.get("publicationTitle", ""),
        "attachment_key": attachment_key,
        "pdfs": [str(pdf) for pdf in pdfs],
    }


def ping_status() -> dict[str, Any]:
    items = zotero_get("users/0/items", {"limit": 1})
    storage = storage_root()
    result: dict[str, Any] = {
        "ok": True,
        "api_base": api_base(),
        "api_candidates": local_api_candidates(),
        "platform": zotero_runtime.platform_name(),
        "config_path": str(zotero_runtime.config_path()),
        "storage": str(storage),
        "storage_exists": storage.exists(),
        "pdf_tools": {},
    }
    for tool in ("pdftotext", "pdftoppm"):
        resolved = resolve_pdf_tool(tool)
        result["pdf_tools"][tool] = str(resolved) if resolved else None
    if items:
        first = items[0]
        library = first.get("library", {})
        result["library"] = {
            "name": library.get("name", ""),
            "id": library.get("id", "0"),
        }
        result["sample_item"] = first
    return result


def format_ping_status(
    result: dict[str, Any], sample_summary: dict[str, Any] | None = None
) -> str:
    lines = [
        f"OK: connected to {result['api_base']}",
        f"platform: {result['platform']}",
        f"storage: {result['storage']}",
        f"storage_exists: {result['storage_exists']}",
    ]
    for tool in ("pdftotext", "pdftoppm"):
        lines.append(f"{tool}: {result['pdf_tools'].get(tool) or 'not found'}")
    library = result.get("library")
    if library:
        lines.append(f"library: {library.get('name', '')} ({library.get('id', '0')})")
        summary = sample_summary or item_summary(result["sample_item"])
        lines.append(format_item_summary(summary))
    return "\n".join(lines)


def cmd_ping(_: argparse.Namespace) -> None:
    print(format_ping_status(ping_status()))


def search_items(
    query: str, limit: int = 5, item_type: str | None = "-attachment"
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "q": query,
        "limit": limit,
        "sort": "dateModified",
        "direction": "desc",
    }
    if item_type:
        params["itemType"] = item_type
    return zotero_get("users/0/items", params) or []


def format_search_items(items: list[dict[str, Any]]) -> str:
    return format_item_summaries(
        [item_summary(item) for item in items], "No matching Zotero items."
    )


def cmd_search(args: argparse.Namespace) -> None:
    items = search_items(args.query, args.limit, args.item_type)
    if not args.json:
        print(format_search_items(items))
        return
    for item in items:
        print_item(item, show_json=True)


def match_items(
    query: str,
    *,
    field: str = "title",
    limit: int = 20,
    scan_limit: int = 1000,
    threshold: float = 0.85,
    best: bool = True,
) -> dict[str, Any]:
    params = {
        "q": query,
        "limit": limit,
        "sort": "dateModified",
        "direction": "desc",
        "itemType": "-attachment",
    }
    items = zotero_get("users/0/items", params) or []
    if (not items or field == "doi") and scan_limit > 0:
        local_items = fetch_items_for_local_match(scan_limit)
        if local_items:
            items = local_items
    if not items:
        return {
            "query": query,
            "field": field,
            "threshold": threshold,
            "threshold_reached": False,
            "matches": [],
        }

    ranked = sorted(
        ((score_item(item, query, field), item) for item in items),
        key=lambda pair: pair[0],
        reverse=True,
    )
    matches = [
        {"match_score": score, "item": item}
        for score, item in ranked
        if score >= threshold
    ]
    if best and matches:
        matches = matches[:1]
    threshold_reached = bool(matches)
    if not matches:
        score, item = ranked[0]
        matches = [{"match_score": score, "item": item}]
    return {
        "query": query,
        "field": field,
        "threshold": threshold,
        "threshold_reached": threshold_reached,
        "matches": matches,
    }


def summarize_match_result(result: dict[str, Any]) -> dict[str, Any]:
    summarized = dict(result)
    summarized["matches"] = [
        {
            "match_score": match["match_score"],
            "item": item_summary(match["item"]),
        }
        for match in result["matches"]
    ]
    return summarized


def format_match_summaries(result: dict[str, Any]) -> str:
    matches = result["matches"]
    if not matches:
        return "No matching Zotero items."
    lines: list[str] = []
    if not result["threshold_reached"]:
        lines.append(
            f"No item reached threshold {result['threshold']:.3f}. Best candidate:"
        )
    for match in matches:
        lines.append(f"match_score: {match['match_score']:.3f}")
        lines.append(format_item_summary(match["item"]))
    return "\n".join(lines)


def format_match_items(result: dict[str, Any], show_json: bool = False) -> str:
    if show_json:
        matches = result["matches"]
        if not matches:
            return "No matching Zotero items."
        lines: list[str] = []
        if not result["threshold_reached"]:
            lines.append(
                f"No item reached threshold {result['threshold']:.3f}. Best candidate:"
            )
        for match in matches:
            lines.append(f"match_score: {match['match_score']:.3f}")
            lines.append(json.dumps(match["item"], ensure_ascii=False, indent=2))
        return "\n".join(lines)
    return format_match_summaries(summarize_match_result(result))


def cmd_match(args: argparse.Namespace) -> None:
    result = match_items(
        args.query,
        field=args.field,
        limit=args.limit,
        scan_limit=args.scan_limit,
        threshold=args.threshold,
        best=args.best,
    )
    print(format_match_items(result, show_json=args.json))


def get_item(key: str) -> dict[str, Any]:
    return zotero_get(f"users/0/items/{quote(key)}")


def cmd_item(args: argparse.Namespace) -> None:
    item = get_item(args.key)
    print_item(item, show_json=args.json)


def get_children(key: str) -> list[dict[str, Any]]:
    return zotero_get(f"users/0/items/{quote(key)}/children") or []


def format_children(children: list[dict[str, Any]]) -> str:
    return format_item_summaries(
        [item_summary(child) for child in children], "No child items."
    )


def cmd_children(args: argparse.Namespace) -> None:
    children = get_children(args.key)
    if not args.json:
        print(format_children(children))
        return
    for child in children:
        print_item(child, show_json=True)


def list_collections() -> list[dict[str, Any]]:
    return collection_summaries(fetch_all_collections())


def format_collections(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return "No Zotero collections."
    lines: list[str] = []
    for summary in summaries:
        lines.extend([f"- key: {summary['key']}", f"  path: {summary['path']}"])
        if summary["parent_key"]:
            lines.append(f"  parent_key: {summary['parent_key']}")
        if summary["num_items"] is not None:
            lines.append(f"  num_items: {summary['num_items']}")
        if summary["num_collections"] is not None:
            lines.append(f"  num_collections: {summary['num_collections']}")
    return "\n".join(lines)


def cmd_collections(args: argparse.Namespace) -> None:
    summaries = list_collections()
    if args.json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return
    print(format_collections(summaries))


def get_item_collections(key: str) -> dict[str, Any]:
    requested_item = get_item(key)
    item = resolve_top_level_item(requested_item)
    collections_by_key = collection_index(fetch_all_collections())
    summaries = item_collection_summaries(item, collections_by_key)
    data = item.get("data", {})
    return {
        "requested_key": key,
        "item_key": data.get("key") or item.get("key", ""),
        "title": data.get("title", ""),
        "collections": summaries,
    }


def format_item_collections(result: dict[str, Any]) -> str:
    lines = [f"item_key: {result['item_key']}"]
    if result["item_key"] != result["requested_key"]:
        lines.append(f"requested_key: {result['requested_key']}")
    if result["title"]:
        lines.append(f"title: {result['title']}")
    summaries = result["collections"]
    if not summaries:
        lines.append("collections: none")
        return "\n".join(lines)
    lines.append("collections:")
    for summary in summaries:
        lines.extend([f"- key: {summary['key']}", f"  path: {summary['path']}"])
    return "\n".join(lines)


def cmd_item_collections(args: argparse.Namespace) -> None:
    result = get_item_collections(args.key)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(format_item_collections(result))


def list_collection_items(
    key: str,
    *,
    recursive: bool = False,
    limit: int = 100,
    collections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if limit < 1:
        raise SystemExit("limit must be >= 1")
    collection_rows = fetch_all_collections() if collections is None else collections
    collections_by_key = collection_index(collection_rows)
    collection = collections_by_key.get(key)
    if not collection:
        raise SystemExit(f"Unknown Zotero collection key: {key}")

    root_summary = collection_summary(collection, collections_by_key)
    items, memberships = collect_collection_items(
        key, collections_by_key, recursive, limit
    )
    rows = []
    for item in items:
        item_key = str(item.get("data", {}).get("key") or item.get("key") or "")
        summary = item_summary(item)
        summary["collection_keys"] = memberships.get(item_key, [])
        summary["collection_paths"] = [
            collection_path(collection_key, collections_by_key)
            for collection_key in memberships.get(item_key, [])
        ]
        rows.append(summary)
    return {
        "collection": root_summary,
        "recursive": recursive,
        "count": len(rows),
        "items": rows,
    }


def format_collection_items(result: dict[str, Any]) -> str:
    root = result["collection"]
    lines = [
        f"collection_key: {root['key']}",
        f"collection_path: {root['path']}",
        f"recursive: {str(result['recursive']).lower()}",
        f"count: {result['count']}",
    ]
    if not result["items"]:
        lines.append("No items in this collection.")
        return "\n".join(lines)
    for summary in result["items"]:
        lines.append(format_item_summary(summary))
        if summary["collection_paths"]:
            lines.append(
                f"  collection_paths: {' | '.join(summary['collection_paths'])}"
            )
    return "\n".join(lines)


def cmd_collection_items(args: argparse.Namespace) -> None:
    result = list_collection_items(args.key, recursive=args.recursive, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(format_collection_items(result))


def cmd_extract_text(args: argparse.Namespace) -> None:
    item = zotero_get(f"users/0/items/{quote(args.key)}")
    pdf = first_pdf_for_item(item)
    out_file = resolve_text_output_path(args.output, args.out_dir, item)
    run_command(
        [
            pdf_tool_command("pdftotext"),
            "-layout",
            "-enc",
            "UTF-8",
            str(pdf),
            str(out_file),
        ]
    )
    print(f"pdf: {pdf}")
    print(f"text: {out_file}")


def parse_pages(page_spec: str) -> list[int]:
    pages: list[int] = []
    for part in page_spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    if any(page < 1 for page in pages):
        raise SystemExit("Pages are 1-based and must be >= 1.")
    return sorted(set(pages))


def render_pages(
    key: str,
    pages: str,
    out_dir: str,
    *,
    dpi: int = 180,
    image_format: str = "png",
) -> dict[str, Any]:
    if not out_dir:
        raise SystemExit("Missing required argument: --out-dir")
    item = get_item(key)
    pdf = first_pdf_for_item(item)
    output_dir = ensure_output_dir(Path(out_dir) / output_stem(item))
    selected_pages = parse_pages(pages)
    written: list[Path] = []
    for page in selected_pages:
        prefix = output_dir / f"page_{page:03d}"
        run_command(
            [
                pdf_tool_command("pdftoppm"),
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                str(dpi),
                f"-{image_format}",
                str(pdf),
                str(prefix),
            ]
        )
        suffix = "jpg" if image_format == "jpeg" else image_format
        candidates = sorted(output_dir.glob(f"page_{page:03d}*.{suffix}"))
        written.extend(candidates)
    return {
        "item_key": key,
        "pdf": str(pdf),
        "pages": selected_pages,
        "dpi": dpi,
        "format": image_format,
        "images": [str(path) for path in written],
    }


def format_render_pages(result: dict[str, Any]) -> str:
    lines = [f"pdf: {result['pdf']}"]
    lines.extend(f"image: {path}" for path in result["images"])
    return "\n".join(lines)


def cmd_render_pages(args: argparse.Namespace) -> None:
    result = render_pages(
        args.key,
        args.pages,
        args.out_dir,
        dpi=args.dpi,
        image_format=args.format,
    )
    print(format_render_pages(result))


def pdf_pages_text(pdf: Path) -> list[tuple[int, str]]:
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        run_command(
            [
                pdf_tool_command("pdftotext"),
                "-bbox-layout",
                "-enc",
                "UTF-8",
                str(pdf),
                str(tmp_path),
            ]
        )
        text = tmp_path.read_text(errors="replace")
    finally:
        tmp_path.unlink(missing_ok=True)

    pages: list[tuple[int, str]] = []
    pattern = re.compile(r"<page\b[^>]*>(.*?)</page>", re.DOTALL | re.IGNORECASE)
    word_pattern = re.compile(r"<word\b[^>]*>(.*?)</word>", re.DOTALL | re.IGNORECASE)
    for index, page_match in enumerate(pattern.finditer(text), start=1):
        words = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", word)).strip()
            for word in word_pattern.findall(page_match.group(1))
        ]
        pages.append((index, " ".join(word for word in words if word)))
    return pages


def figure_patterns(label: str) -> list[re.Pattern[str]]:
    label = label.strip()
    wants_extended = "extended" in label.casefold()
    escaped = re.escape(label)
    patterns = [escaped]
    fig_match = re.fullmatch(r"(?:fig(?:ure)?\.?\s*)?(\d+[a-z]?)", label, re.IGNORECASE)
    if fig_match:
        figure_id = re.escape(fig_match.group(1))
        patterns.extend(
            [
                rf"(?<!Extended\sData\s)\bFig\.\s*{figure_id}\b",
                rf"\bFigure\s+{figure_id}\b",
            ]
        )
    ext_match = re.fullmatch(
        r"(?:extended\s+data\s+)?(?:fig(?:ure)?\.?\s*)?(\d+[a-z]?)",
        label,
        re.IGNORECASE,
    )
    if wants_extended and ext_match:
        figure_id = re.escape(ext_match.group(1))
        patterns.append(rf"\bExtended\s+Data\s+Fig\.\s*{figure_id}\b")
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def figure_caption_patterns(label: str) -> list[re.Pattern[str]]:
    patterns: list[str] = []
    wants_extended = "extended" in label.casefold()
    fig_match = re.fullmatch(r"(?:fig(?:ure)?\.?\s*)?(\d+[a-z]?)", label, re.IGNORECASE)
    if fig_match and not wants_extended:
        figure_id = re.escape(fig_match.group(1))
        patterns.extend(
            [
                rf"(?<!Extended\sData\s)\bFig\.\s*{figure_id}\s*\|",
                rf"\bFigure\s+{figure_id}\s*\|",
            ]
        )
    ext_match = re.fullmatch(
        r"(?:extended\s+data\s+)?(?:fig(?:ure)?\.?\s*)?(\d+[a-z]?)",
        label,
        re.IGNORECASE,
    )
    if wants_extended and ext_match:
        figure_id = re.escape(ext_match.group(1))
        patterns.append(rf"\bExtended\s+Data\s+Fig\.\s*{figure_id}\s*\|")
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def find_figure_pages(
    key: str, figure: str, *, limit: int = 10, context_chars: int = 180
) -> dict[str, Any]:
    item = get_item(key)
    pdf = first_pdf_for_item(item)
    pages = pdf_pages_text(pdf)
    patterns = figure_patterns(figure)
    caption_patterns = figure_caption_patterns(figure)
    ranked_matches: list[tuple[int, int, str, str]] = []
    for page, text in pages:
        page_matches: list[tuple[int, str, re.Match[str]]] = []
        for pattern in caption_patterns:
            match = pattern.search(text)
            if match:
                page_matches.append((0, "caption", match))
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                page_matches.append((1, "mention", match))
        if not page_matches:
            continue
        priority, match_type, match = min(
            page_matches, key=lambda item: (item[0], item[2].start())
        )
        start = max(0, match.start() - context_chars)
        end = min(len(text), match.end() + context_chars)
        context = re.sub(r"\s+", " ", text[start:end]).strip()
        ranked_matches.append((priority, page, match_type, context))

    matches = [
        {"page": page, "match_type": match_type, "context": text}
        for _, page, match_type, text in sorted(ranked_matches)[:limit]
    ]
    return {
        "item_key": key,
        "figure": figure,
        "pdf": str(pdf),
        "count": len(matches),
        "matches": matches,
    }


def format_figure_pages(result: dict[str, Any]) -> str:
    lines = [f"pdf: {result['pdf']}"]
    if not result["matches"]:
        lines.append(f"No page match found for figure: {result['figure']}")
        return "\n".join(lines)
    for match in result["matches"]:
        lines.extend(
            [
                f"page: {match['page']}",
                f"match_type: {match['match_type']}",
                f"context: {match['context']}",
            ]
        )
    return "\n".join(lines)


def cmd_find_figure_pages(args: argparse.Namespace) -> None:
    result = find_figure_pages(
        args.key,
        args.figure,
        limit=args.limit,
        context_chars=args.context,
    )
    print(format_figure_pages(result))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only helper for the configured Zotero Local API."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ping = subparsers.add_parser("ping", help="test Zotero Local API access")
    ping.set_defaults(func=cmd_ping)

    search = subparsers.add_parser("search", help="search Zotero items")
    search.add_argument("query", help="title, author, DOI, or keyword")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--item-type", default="-attachment")
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    match = subparsers.add_parser(
        "match", help="find the best Zotero item by title or DOI"
    )
    match.add_argument("query", help="exact or near-exact title/DOI")
    match.add_argument("--field", choices=["title", "doi"], default="title")
    match.add_argument("--limit", type=int, default=20)
    match.add_argument("--scan-limit", type=int, default=1000)
    match.add_argument("--threshold", type=float, default=0.85)
    match.add_argument(
        "--best", action="store_true", help="show only the best candidate"
    )
    match.add_argument("--json", action="store_true")
    match.set_defaults(func=cmd_match)

    item = subparsers.add_parser("item", help="show one Zotero item by key")
    item.add_argument("key")
    item.add_argument("--json", action="store_true")
    item.set_defaults(func=cmd_item)

    children = subparsers.add_parser("children", help="show child items by item key")
    children.add_argument("key")
    children.add_argument("--json", action="store_true")
    children.set_defaults(func=cmd_children)

    collections = subparsers.add_parser(
        "collections", help="list Zotero collection paths"
    )
    collections.add_argument("--json", action="store_true")
    collections.set_defaults(func=cmd_collections)

    item_collections = subparsers.add_parser(
        "item-collections", help="show collection memberships for one Zotero item"
    )
    item_collections.add_argument("key")
    item_collections.add_argument("--json", action="store_true")
    item_collections.set_defaults(func=cmd_item_collections)

    collection_items = subparsers.add_parser(
        "collection-items", help="list items assigned to one Zotero collection"
    )
    collection_items.add_argument("key")
    collection_items.add_argument("--recursive", action="store_true")
    collection_items.add_argument("--limit", type=int, default=100)
    collection_items.add_argument("--json", action="store_true")
    collection_items.set_defaults(func=cmd_collection_items)

    extract_text = subparsers.add_parser(
        "extract-text", help="extract full PDF text for one Zotero parent item"
    )
    extract_text.add_argument("key")
    extract_text.add_argument("--out-dir")
    extract_text.add_argument("--output")
    extract_text.set_defaults(func=cmd_extract_text)

    render_pages = subparsers.add_parser(
        "render-pages", help="render selected PDF pages as images"
    )
    render_pages.add_argument("key")
    render_pages.add_argument(
        "--pages", required=True, help="1-based pages, e.g. 3 or 3-5,8"
    )
    render_pages.add_argument("--out-dir", required=True)
    render_pages.add_argument("--dpi", type=int, default=180)
    render_pages.add_argument("--format", choices=["png", "jpeg"], default="png")
    render_pages.set_defaults(func=cmd_render_pages)

    find_figure_pages = subparsers.add_parser(
        "find-figure-pages", help="find PDF pages that mention a figure label"
    )
    find_figure_pages.add_argument("key")
    find_figure_pages.add_argument(
        "figure", help="figure label, e.g. 'Fig. 2' or 'Extended Data Fig. 1'"
    )
    find_figure_pages.add_argument("--limit", type=int, default=10)
    find_figure_pages.add_argument("--context", type=int, default=180)
    find_figure_pages.set_defaults(func=cmd_find_figure_pages)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
