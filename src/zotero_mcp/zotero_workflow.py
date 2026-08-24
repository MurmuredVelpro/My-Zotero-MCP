#!/usr/bin/env python3
"""Read-only Zotero workflow inventory backed by a local SQLite database."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from . import (
    mineru_client,
    mineru_qmd_pipeline,
    workflow_database,
    zotero_collections,
    zotero_local,
    zotero_translate,
)

SCHEMA_VERSION = workflow_database.SCHEMA_VERSION
DEFAULT_EXPORT_NAME = "zotero_workflow_status.csv"
ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
AUTOMATED_TRANSLATION_RE = re.compile(
    r"^(?P<item_key>[A-Z0-9]{8})_(?P<attachment_key>[A-Z0-9]{8})_"
)
SUPPLEMENTARY_RE = re.compile(
    r"(?:^|[\s_.-])(?:si|supp(?:lement(?:ary)?)?)(?:$|[\s_.-])",
    re.IGNORECASE,
)

EXPORT_FIELDS = (
    "item_key",
    "title",
    "date_added",
    "collection_paths",
    "source_attachment_key",
    "source_attachment_title",
    "translation_state",
    "translation_attachment_key",
    "mineru_state",
    "parsed_attachment_key",
    "qmd_state",
    "qmd_document_ref",
    "issue",
    "last_seen_at",
)


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkflowSnapshot:
    observed_at: str
    roots: list[dict[str, Any]]
    items: list[dict[str, Any]]
    memberships: list[dict[str, str]]
    attachments: list[dict[str, Any]]
    mineru_documents: list[dict[str, Any]]
    qmd_documents: list[dict[str, Any]]
    translation_queue: list[dict[str, Any]]
    pdf2zh_tasks: list[dict[str, Any]]
    system_health: list[dict[str, Any]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_database_path() -> Path:
    return workflow_database.default_database_path()


def default_export_path() -> Path:
    return zotero_translate.state_dir() / DEFAULT_EXPORT_NAME


def collection_reference(value: str) -> dict[str, str]:
    value = value.strip()
    if not value:
        raise WorkflowError("collection reference must not be empty")
    if ITEM_KEY_RE.fullmatch(value):
        return {"key": value}
    if " > " in value:
        return {"path": value}
    return {"name": value}


def _is_pdf(data: dict[str, Any]) -> bool:
    filename = str(data.get("filename") or "")
    return data.get("contentType") == "application/pdf" or filename.lower().endswith(
        ".pdf"
    )


def _is_supplementary(title: str, filename: str) -> bool:
    cleaned = title.strip().casefold()
    if cleaned in {
        "si",
        "supplement",
        "supplementary",
        "supplementary material",
        "补充材料",
    }:
        return True
    return bool(SUPPLEMENTARY_RE.search(title) or SUPPLEMENTARY_RE.search(filename))


def _qmd_documents() -> dict[str, dict[str, str]]:
    collection = mineru_qmd_pipeline.qmd_collection()
    result = subprocess.run(
        [
            mineru_qmd_pipeline.qmd_command(),
            "multi-get",
            f"qmd://{collection}/*/full.md",
            "--format",
            "json",
            "--max-bytes",
            "1",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise WorkflowError(
            f"QMD multi-get failed with exit code {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError("QMD multi-get returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise WorkflowError("QMD multi-get returned invalid data")

    documents: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        file_name = str(row.get("file") or "")
        prefix = f"qmd://{collection}/"
        if file_name.startswith(prefix):
            file_name = file_name.removeprefix(prefix)
        match = re.fullmatch(r"([^/]+)/full\.md", file_name)
        if not match:
            continue
        item_key = match.group(1)
        if item_key in documents:
            raise WorkflowError(f"duplicate QMD document for item: {item_key}")
        documents[item_key] = {
            "document_ref": f"qmd://{collection}/{file_name}",
            "docid": str(row.get("docid") or ""),
        }
    return documents


def _pdf2zh_status() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = zotero_translate.load_pdf2zh_settings()
    client = zotero_translate.PDF2ZHClient()
    base_url = client.health(settings)
    history_response = client.session.get(f"{base_url}/api/history", timeout=10)
    history_response.raise_for_status()
    history_payload = history_response.json()
    history = (
        history_payload.get("history") if isinstance(history_payload, dict) else None
    )
    if not isinstance(history, list):
        raise WorkflowError("PDF2zh history returned invalid data")

    active: list[dict[str, Any]] = []
    with client.session.get(
        f"{base_url}/events", stream=True, timeout=(5, 5)
    ) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines(decode_unicode=True):
            line = str(raw_line or "").strip()
            if not line.startswith("data:"):
                continue
            payload = json.loads(line.removeprefix("data:").strip())
            if isinstance(payload, dict) and payload.get("type") == "tasks":
                data = payload.get("data")
                if not isinstance(data, list):
                    raise WorkflowError("PDF2zh events returned invalid task data")
                active = data
                break

    tasks: list[dict[str, Any]] = []
    for task in active:
        if not isinstance(task, dict):
            continue
        tasks.append(
            {
                "task_key": str(task.get("taskId") or ""),
                "filename": str(task.get("fileName") or ""),
                "active": 1,
                "status": str(task.get("status") or ""),
                "started_at": str(task.get("startTime") or ""),
                "ended_at": "",
            }
        )
    for index, task in enumerate(history):
        if not isinstance(task, dict):
            continue
        filename = str(task.get("fileName") or "")
        started_at = str(task.get("startTime") or "")
        tasks.append(
            {
                "task_key": f"history:{started_at}:{filename}:{index}",
                "filename": filename,
                "active": 0,
                "status": str(task.get("status") or ""),
                "started_at": started_at,
                "ended_at": str(task.get("endTime") or ""),
            }
        )
    return tasks, {
        "base_url": base_url,
        "active_count": len(active),
        "history_count": len(history),
    }


def _translation_queue_rows() -> list[dict[str, str]]:
    return zotero_translate.QueueStore(zotero_translate.default_queue_path()).read()


def _rename_watch_detail() -> dict[str, Any]:
    path = zotero_translate.default_rename_watch_state_path()
    if not path.is_file():
        return {"path": str(path), "present": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WorkflowError("manual translation rename state is invalid")
    pending = data.get("pending")
    return {
        "path": str(path),
        "present": True,
        "last_version": data.get("last_version"),
        "pending_count": len(pending) if isinstance(pending, dict) else 0,
    }


def _map_task_to_source(
    filename: str,
    source_by_filename: dict[str, list[tuple[str, str]]],
    attachment_keys: set[str],
) -> tuple[str | None, str | None]:
    match = AUTOMATED_TRANSLATION_RE.match(filename)
    if match:
        item_key = match.group("item_key")
        attachment_key = match.group("attachment_key")
        if attachment_key in attachment_keys:
            return item_key, attachment_key
    matches = source_by_filename.get(filename, [])
    if len(matches) == 1:
        return matches[0]
    return None, None


def _pipeline_gate_states(
    mineru_record: dict[str, str],
    source_key: str | None,
    full_md_present: bool,
    qmd_known: bool,
    qmd_document_present: bool,
) -> tuple[str, str, list[str]]:
    issues: list[str] = []
    parsed_key = str(mineru_record.get("parsed_attachment_key") or "")
    mineru_parsed = mineru_record.get("mineru_parsed") == "true"
    qmd_indexed = mineru_record.get("qmd_indexed") == "true"

    if not full_md_present:
        mineru_state = "missing"
    elif not parsed_key:
        mineru_state = "present_unknown_source"
        issues.append("mineru_source_unknown")
    elif not source_key:
        mineru_state = "source_missing"
        issues.append("mineru_source_missing")
    elif parsed_key != source_key:
        mineru_state = "stale_source"
        issues.append("mineru_stale_source")
    elif not mineru_parsed:
        mineru_state = "pending_parse"
        issues.append("mineru_parse_unconfirmed")
    else:
        mineru_state = "parsed_current"

    if not qmd_known:
        qmd_state = "unknown"
    elif mineru_state == "parsed_current" and not qmd_indexed:
        qmd_state = "pending_index"
        issues.append("qmd_index_pending")
    elif qmd_indexed and not qmd_document_present:
        qmd_state = "missing"
        issues.append("qmd_document_missing")
    elif mineru_state == "parsed_current":
        qmd_state = "indexed_current"
    elif mineru_state == "missing":
        qmd_state = "source_missing"
        issues.append("qmd_source_missing")
    else:
        qmd_state = "stale_source"
        issues.append("qmd_stale_source")
    return mineru_state, qmd_state, issues


def _translation_state(
    translation_count: int,
    has_standard_translation: bool,
    active_task: bool,
    queue_state: str,
    source_key: str | None,
) -> tuple[str, list[str]]:
    issues: list[str] = []
    if translation_count > 1:
        return "ambiguous_translation", [f"translation_attachments={translation_count}"]
    if has_standard_translation:
        return "translated_standard", issues
    if translation_count:
        return "translated_needs_normalization", issues
    if active_task:
        return "translating", issues
    if queue_state:
        if queue_state == "done":
            return "done_missing_attachment", [
                "translation_queue_done_but_attachment_missing"
            ]
        mapped = {
            "pending": "queued",
            "waiting": "waiting",
            "retry_wait": "retry_scheduled",
            "translating": "translating",
            "importing": "importing",
            "failed": "failed",
        }.get(queue_state)
        if mapped:
            return mapped, issues
        issues.append(f"translation_queue_state={queue_state}")
    if source_key is None:
        return "no_source_pdf", issues
    return "untranslated", issues


def source_attachment_hints(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        with _connect(path) as connection:
            rows = connection.execute(
                """
                SELECT item_key, source_attachment_key
                FROM items
                WHERE in_scope=1 AND source_attachment_key IS NOT NULL
                """
            ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(row["item_key"]): str(row["source_attachment_key"]) for row in rows}


def build_snapshot(
    collection_values: list[str], source_hints: dict[str, str] | None = None
) -> WorkflowSnapshot:
    observed_at = utc_now()
    source_hints = source_hints or {}
    collection_rows = zotero_local.fetch_all_collections()
    resolver = zotero_collections.CollectionResolver(collection_rows)
    collections_by_key = zotero_local.collection_index(collection_rows)
    roots = [
        resolver.resolve(collection_reference(value)) for value in collection_values
    ]

    items_by_key: dict[str, dict[str, Any]] = {}
    memberships_by_item: dict[str, set[str]] = {}
    for root in roots:
        items, memberships = zotero_local.collect_collection_items(
            root["key"], collections_by_key, recursive=True, limit=1000
        )
        for item in items:
            data = item.get("data") or {}
            item_key = str(data.get("key") or item.get("key") or "")
            if item_key:
                items_by_key[item_key] = item
                memberships_by_item.setdefault(item_key, set()).update(
                    memberships.get(item_key, [])
                )

    scope_keys = set(items_by_key)
    all_attachments = zotero_local.fetch_paginated(
        "users/0/items", {"itemType": "attachment"}
    )
    attachments_by_parent: dict[str, list[dict[str, Any]]] = {}
    for attachment in all_attachments:
        data = attachment.get("data") or {}
        parent_key = str(data.get("parentItem") or "")
        if parent_key in scope_keys and _is_pdf(data):
            attachments_by_parent.setdefault(parent_key, []).append(attachment)

    naming = zotero_translate.load_translation_naming()
    attachment_rows: list[dict[str, Any]] = []
    item_rows: dict[str, dict[str, Any]] = {}
    source_by_filename: dict[str, list[tuple[str, str]]] = {}
    attachment_keys: set[str] = set()
    translations_by_item: dict[str, list[dict[str, Any]]] = {}
    source_hints_reused = 0
    source_language_checks = 0

    for item_key, item in items_by_key.items():
        data = item.get("data") or {}
        primary_key = zotero_local.attachment_key_from_links(item)
        source_key: str | None = None
        source_error = ""
        attachments = attachments_by_parent.get(item_key, [])
        source_hint = source_hints.get(item_key)
        hinted_attachment = next(
            (
                attachment
                for attachment in attachments
                if str(
                    (attachment.get("data") or {}).get("key")
                    or attachment.get("key")
                    or ""
                )
                == source_hint
            ),
            None,
        )
        if hinted_attachment is not None:
            hinted_data = hinted_attachment.get("data") or {}
            hinted_title = str(hinted_data.get("title") or "").strip()
            hinted_filename = str(hinted_data.get("filename") or "")
            if not (
                naming.matches_attachment_title(hinted_title)
                or zotero_local.has_translation_marker(hinted_title, hinted_filename)
                or _is_supplementary(hinted_title, hinted_filename)
            ):
                source_key = source_hint
                source_hints_reused += 1
        if attachments and source_key is None:
            source_language_checks += 1
            try:
                source_key = str(
                    zotero_local.english_pdf_attachment_for_item(item)["key"]
                )
            except (SystemExit, OSError, subprocess.SubprocessError) as exc:
                source_error = str(exc)

        for attachment in attachments_by_parent.get(item_key, []):
            attachment_data = attachment.get("data") or {}
            attachment_key = str(
                attachment_data.get("key") or attachment.get("key") or ""
            )
            title = str(attachment_data.get("title") or "").strip()
            filename = str(attachment_data.get("filename") or "")
            is_standard_translation = naming.matches_attachment_title(title)
            is_translation = (
                is_standard_translation
                or zotero_local.has_translation_marker(title, filename)
            )
            if attachment_key == source_key:
                role = "source_pdf"
                language = "en"
                source_by_filename.setdefault(filename, []).append(
                    (item_key, attachment_key)
                )
            elif is_translation:
                role = "translated_pdf"
                language = "zh"
            elif _is_supplementary(title, filename):
                role = "supplementary_pdf"
                language = "unknown"
            else:
                role = "other_pdf"
                language = "unknown"
            local_paths = zotero_local.find_pdf_for_attachment(attachment_key)
            row = {
                "attachment_key": attachment_key,
                "item_key": item_key,
                "title": title,
                "filename": filename,
                "content_type": str(attachment_data.get("contentType") or ""),
                "link_mode": str(attachment_data.get("linkMode") or ""),
                "role": role,
                "language": language,
                "is_primary": int(attachment_key == primary_key),
                "is_standard_title": int(is_standard_translation),
                "translation_of_attachment_key": source_key
                if role == "translated_pdf"
                else None,
                "local_path": str(local_paths[0]) if local_paths else "",
                "last_seen_at": observed_at,
            }
            attachment_rows.append(row)
            attachment_keys.add(attachment_key)
            if role == "translated_pdf":
                translations_by_item.setdefault(item_key, []).append(row)

        item_rows[item_key] = {
            "item_key": item_key,
            "item_type": str(data.get("itemType") or ""),
            "title": str(data.get("title") or ""),
            "authors": zotero_local.creator_summary(item),
            "date": str(data.get("date") or ""),
            "date_added": str(data.get("dateAdded") or ""),
            "date_modified": str(data.get("dateModified") or ""),
            "doi": str(data.get("DOI") or ""),
            "publication_title": str(data.get("publicationTitle") or ""),
            "zotero_version": int(item.get("version") or 0),
            "source_attachment_key": source_key,
            "source_error": source_error,
            "translation_state": "untranslated",
            "mineru_state": "unknown",
            "qmd_state": "unknown",
            "issue": "",
            "last_seen_at": observed_at,
        }

    health: list[dict[str, Any]] = [
        {
            "system_name": "zotero",
            "status": "ok",
            "detail": {
                "scope_items": len(item_rows),
                "pdf_attachments": len(attachment_rows),
                "source_hints_reused": source_hints_reused,
                "source_language_checks": source_language_checks,
            },
            "checked_at": observed_at,
            "error": "",
        }
    ]

    mineru_records: dict[str, dict[str, str]] = {}
    try:
        mineru_records = workflow_database.mineru_records_by_key()
        health.append(
            {
                "system_name": "mineru_state",
                "status": "ok",
                "detail": {
                    "database": str(default_database_path()),
                    "rows": len(mineru_records),
                },
                "checked_at": observed_at,
                "error": "",
            }
        )
    except (OSError, RuntimeError, sqlite3.DatabaseError) as exc:
        health.append(
            {
                "system_name": "mineru_state",
                "status": "error",
                "detail": {"database": str(default_database_path())},
                "checked_at": observed_at,
                "error": str(exc),
            }
        )

    qmd_known = True
    qmd_rows: dict[str, dict[str, str]] = {}
    try:
        qmd_rows = _qmd_documents()
        health.append(
            {
                "system_name": "qmd",
                "status": "ok",
                "detail": {"documents": len(qmd_rows)},
                "checked_at": observed_at,
                "error": "",
            }
        )
    except (OSError, subprocess.SubprocessError, WorkflowError) as exc:
        qmd_known = False
        health.append(
            {
                "system_name": "qmd",
                "status": "error",
                "detail": {},
                "checked_at": observed_at,
                "error": str(exc),
            }
        )

    queue_rows: list[dict[str, Any]] = []
    try:
        for row in _translation_queue_rows():
            if row["parent_item_key"] not in scope_keys:
                continue
            queue_rows.append({**row, "observed_at": observed_at})
        health.append(
            {
                "system_name": "translation_queue",
                "status": "ok",
                "detail": {
                    "path": str(zotero_translate.default_queue_path()),
                    "rows": len(queue_rows),
                },
                "checked_at": observed_at,
                "error": "",
            }
        )
    except (OSError, zotero_translate.TranslationError) as exc:
        health.append(
            {
                "system_name": "translation_queue",
                "status": "error",
                "detail": {"path": str(zotero_translate.default_queue_path())},
                "checked_at": observed_at,
                "error": str(exc),
            }
        )

    pdf2zh_tasks: list[dict[str, Any]] = []
    try:
        tasks, detail = _pdf2zh_status()
        for task in tasks:
            item_key, source_key = _map_task_to_source(
                task["filename"], source_by_filename, attachment_keys
            )
            pdf2zh_tasks.append(
                {
                    **task,
                    "item_key": item_key,
                    "source_attachment_key": source_key,
                    "observed_at": observed_at,
                }
            )
        health.append(
            {
                "system_name": "pdf2zh",
                "status": "ok",
                "detail": detail,
                "checked_at": observed_at,
                "error": "",
            }
        )
    except (
        OSError,
        ValueError,
        requests.RequestException,
        zotero_translate.TranslationError,
        WorkflowError,
    ) as exc:
        health.append(
            {
                "system_name": "pdf2zh",
                "status": "error",
                "detail": {},
                "checked_at": observed_at,
                "error": str(exc),
            }
        )

    try:
        rename_detail = _rename_watch_detail()
        health.append(
            {
                "system_name": "rename_watch",
                "status": "ok",
                "detail": rename_detail,
                "checked_at": observed_at,
                "error": "",
            }
        )
    except (OSError, ValueError, WorkflowError) as exc:
        health.append(
            {
                "system_name": "rename_watch",
                "status": "error",
                "detail": {},
                "checked_at": observed_at,
                "error": str(exc),
            }
        )

    active_tasks_by_item = {
        task["item_key"] for task in pdf2zh_tasks if task["active"] and task["item_key"]
    }
    queue_by_item = {row["parent_item_key"]: row for row in queue_rows}
    mineru_documents: list[dict[str, Any]] = []
    qmd_documents: list[dict[str, Any]] = []

    for item_key, item_row in item_rows.items():
        source_key = item_row["source_attachment_key"]
        translations = translations_by_item.get(item_key, [])
        standard = [row for row in translations if row["is_standard_title"]]
        queue = queue_by_item.get(item_key)
        translation_state, issues = _translation_state(
            len(translations),
            bool(standard),
            item_key in active_tasks_by_item,
            str(queue.get("status") or "") if queue else "",
            source_key,
        )

        if item_row["source_error"]:
            issues.append(f"source_selection={item_row['source_error']}")

        mineru_record = mineru_records.get(item_key, {})
        parsed_key = str(mineru_record.get("parsed_attachment_key") or "")
        output_dir = mineru_client.DEFAULT_OUTPUT_ROOT / item_key
        full_md = output_dir / "full.md"
        full_md_present = full_md.is_file() and full_md.stat().st_size > 0
        mineru_state, qmd_state, pipeline_issues = _pipeline_gate_states(
            mineru_record,
            source_key,
            full_md_present,
            qmd_known,
            item_key in qmd_rows,
        )
        issues.extend(pipeline_issues)
        mineru_documents.append(
            {
                "item_key": item_key,
                "parsed_attachment_key": parsed_key or None,
                "full_md_path": str(full_md) if full_md_present else "",
                "state": mineru_state,
                "checked_at": observed_at,
            }
        )

        qmd_document = qmd_rows.get(item_key)
        qmd_documents.append(
            {
                "item_key": item_key,
                "document_ref": qmd_document["document_ref"] if qmd_document else "",
                "docid": qmd_document["docid"] if qmd_document else "",
                "state": qmd_state,
                "checked_at": observed_at,
            }
        )

        item_row["translation_state"] = translation_state
        item_row["mineru_state"] = mineru_state
        item_row["qmd_state"] = qmd_state
        item_row["issue"] = "; ".join(issues)

    memberships: list[dict[str, str]] = []
    for item_key, collection_keys in memberships_by_item.items():
        for collection_key in sorted(collection_keys):
            memberships.append(
                {
                    "item_key": item_key,
                    "collection_key": collection_key,
                    "collection_path": zotero_local.collection_path(
                        collection_key, collections_by_key
                    ),
                }
            )

    return WorkflowSnapshot(
        observed_at=observed_at,
        roots=roots,
        items=list(item_rows.values()),
        memberships=memberships,
        attachments=attachment_rows,
        mineru_documents=mineru_documents,
        qmd_documents=qmd_documents,
        translation_queue=queue_rows,
        pdf2zh_tasks=pdf2zh_tasks,
        system_health=health,
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_collections (
    collection_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    recursive INTEGER NOT NULL CHECK (recursive IN (0, 1))
);
CREATE TABLE IF NOT EXISTS items (
    item_key TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    authors TEXT NOT NULL,
    date TEXT NOT NULL,
    date_added TEXT NOT NULL,
    date_modified TEXT NOT NULL,
    doi TEXT NOT NULL,
    publication_title TEXT NOT NULL,
    zotero_version INTEGER NOT NULL,
    source_attachment_key TEXT,
    source_error TEXT NOT NULL,
    translation_state TEXT NOT NULL,
    mineru_state TEXT NOT NULL,
    qmd_state TEXT NOT NULL,
    issue TEXT NOT NULL,
    in_scope INTEGER NOT NULL CHECK (in_scope IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_collections (
    item_key TEXT NOT NULL REFERENCES items(item_key),
    collection_key TEXT NOT NULL,
    collection_path TEXT NOT NULL,
    PRIMARY KEY (item_key, collection_key)
);
CREATE TABLE IF NOT EXISTS pdf_attachments (
    attachment_key TEXT PRIMARY KEY,
    item_key TEXT NOT NULL REFERENCES items(item_key),
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    link_mode TEXT NOT NULL,
    role TEXT NOT NULL,
    language TEXT NOT NULL,
    is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
    is_standard_title INTEGER NOT NULL CHECK (is_standard_title IN (0, 1)),
    translation_of_attachment_key TEXT,
    local_path TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mineru_documents (
    item_key TEXT PRIMARY KEY REFERENCES items(item_key),
    parsed_attachment_key TEXT,
    full_md_path TEXT NOT NULL,
    state TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qmd_documents (
    item_key TEXT PRIMARY KEY REFERENCES items(item_key),
    document_ref TEXT NOT NULL,
    docid TEXT NOT NULL,
    state TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translation_queue (
    item_key TEXT PRIMARY KEY REFERENCES items(item_key),
    source_attachment_key TEXT NOT NULL,
    state TEXT NOT NULL,
    output_pdf TEXT NOT NULL,
    last_error TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT NOT NULL DEFAULT '',
    next_attempt_at TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pdf2zh_tasks (
    task_key TEXT PRIMARY KEY,
    item_key TEXT,
    source_attachment_key TEXT,
    filename TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_health (
    system_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    error TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS item_collections_path_idx
    ON item_collections(collection_path);
CREATE INDEX IF NOT EXISTS pdf_attachments_item_idx
    ON pdf_attachments(item_key);
"""


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def store_snapshot(path: Path, snapshot: WorkflowSnapshot) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.executescript(workflow_database.MINERU_SCHEMA_SQL)
        workflow_database.ensure_translation_queue_columns(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("UPDATE items SET in_scope = 0")
        for table in (
            "item_collections",
            "pdf_attachments",
            "mineru_documents",
            "qmd_documents",
            "translation_queue",
            "pdf2zh_tasks",
            "system_health",
            "tracked_collections",
        ):
            connection.execute(f"DELETE FROM {table}")

        connection.executemany(
            """
            INSERT INTO tracked_collections(collection_key, name, path, recursive)
            VALUES(:key, :name, :path, 1)
            """,
            snapshot.roots,
        )
        connection.executemany(
            """
            INSERT INTO items(
                item_key, item_type, title, authors, date, date_added, date_modified,
                doi, publication_title, zotero_version, source_attachment_key,
                source_error, translation_state, mineru_state, qmd_state, issue,
                in_scope, first_seen_at, last_seen_at
            ) VALUES(
                :item_key, :item_type, :title, :authors, :date, :date_added,
                :date_modified, :doi, :publication_title, :zotero_version,
                :source_attachment_key, :source_error, :translation_state,
                :mineru_state, :qmd_state, :issue, 1, :last_seen_at, :last_seen_at
            )
            ON CONFLICT(item_key) DO UPDATE SET
                item_type=excluded.item_type,
                title=excluded.title,
                authors=excluded.authors,
                date=excluded.date,
                date_added=excluded.date_added,
                date_modified=excluded.date_modified,
                doi=excluded.doi,
                publication_title=excluded.publication_title,
                zotero_version=excluded.zotero_version,
                source_attachment_key=excluded.source_attachment_key,
                source_error=excluded.source_error,
                translation_state=excluded.translation_state,
                mineru_state=excluded.mineru_state,
                qmd_state=excluded.qmd_state,
                issue=excluded.issue,
                in_scope=1,
                last_seen_at=excluded.last_seen_at
            """,
            snapshot.items,
        )
        connection.executemany(
            """
            INSERT INTO item_collections(item_key, collection_key, collection_path)
            VALUES(:item_key, :collection_key, :collection_path)
            """,
            snapshot.memberships,
        )
        connection.executemany(
            """
            INSERT INTO pdf_attachments(
                attachment_key, item_key, title, filename, content_type, link_mode,
                role, language, is_primary, is_standard_title,
                translation_of_attachment_key, local_path, last_seen_at
            ) VALUES(
                :attachment_key, :item_key, :title, :filename, :content_type,
                :link_mode, :role, :language, :is_primary, :is_standard_title,
                :translation_of_attachment_key, :local_path, :last_seen_at
            )
            """,
            snapshot.attachments,
        )
        connection.executemany(
            """
            INSERT INTO mineru_documents(
                item_key, parsed_attachment_key, full_md_path, state, checked_at
            ) VALUES(
                :item_key, :parsed_attachment_key, :full_md_path, :state, :checked_at
            )
            """,
            snapshot.mineru_documents,
        )
        connection.executemany(
            """
            INSERT INTO qmd_documents(item_key, document_ref, docid, state, checked_at)
            VALUES(:item_key, :document_ref, :docid, :state, :checked_at)
            """,
            snapshot.qmd_documents,
        )
        connection.executemany(
            """
            INSERT INTO translation_queue(
                item_key, source_attachment_key, state, output_pdf, last_error,
                attempt_count, downloaded_at, next_attempt_at, observed_at
            ) VALUES(
                :parent_item_key, :source_attachment_key, :status, :output_pdf,
                :last_error, :attempt_count, :downloaded_at, :next_attempt_at,
                :observed_at
            )
            """,
            snapshot.translation_queue,
        )
        connection.executemany(
            """
            INSERT INTO pdf2zh_tasks(
                task_key, item_key, source_attachment_key, filename, active, status,
                started_at, ended_at, observed_at
            ) VALUES(
                :task_key, :item_key, :source_attachment_key, :filename, :active,
                :status, :started_at, :ended_at, :observed_at
            )
            """,
            snapshot.pdf2zh_tasks,
        )
        connection.executemany(
            """
            INSERT INTO system_health(
                system_name, status, detail_json, checked_at, error
            ) VALUES(:system_name, :status, :detail_json, :checked_at, :error)
            """,
            [
                {**row, "detail_json": json.dumps(row["detail"], ensure_ascii=False)}
                for row in snapshot.system_health
            ],
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('last_sync_at', ?)",
            (snapshot.observed_at,),
        )
    if os.name != "nt":
        os.chmod(path, 0o600)


def tracked_collection_values(path: Path) -> list[str]:
    if not path.is_file():
        raise WorkflowError(
            "workflow database does not exist; first sync requires --collection"
        )
    with _connect(path) as connection:
        try:
            rows = connection.execute(
                "SELECT collection_key FROM tracked_collections ORDER BY path"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise WorkflowError(f"invalid workflow database: {exc}") from exc
    if not rows:
        raise WorkflowError("workflow database has no tracked collections")
    return [str(row["collection_key"]) for row in rows]


def next_batch_data(path: Path, collection: str, limit: int = 5) -> dict[str, Any]:
    if limit < 1:
        raise WorkflowError("batch limit must be at least 1")
    if not path.is_file():
        raise WorkflowError(f"workflow database does not exist: {path}")

    reference = collection_reference(collection)
    field, value = next(iter(reference.items()))
    column = {"key": "collection_key", "name": "name", "path": "path"}[field]
    with _connect(path) as connection:
        roots = connection.execute(
            f"SELECT collection_key, name, path FROM tracked_collections "
            f"WHERE {column}=? ORDER BY path",
            (value,),
        ).fetchall()
        if not roots:
            raise WorkflowError(f"tracked collection not found: {collection}")
        if len(roots) > 1:
            paths = ", ".join(str(row["path"]) for row in roots)
            raise WorkflowError(
                f"tracked collection is ambiguous: {collection}; matches: {paths}"
            )

        root = roots[0]
        collection_key = str(root["collection_key"])
        last_sync = connection.execute(
            "SELECT value FROM metadata WHERE key='last_sync_at'"
        ).fetchone()
        counts = connection.execute(
            """
            SELECT
                COUNT(*) AS direct_items,
                SUM(
                    i.mineru_state='parsed_current'
                    AND i.qmd_state='indexed_current'
                ) AS reviewable_items
            FROM items i
            JOIN item_collections c ON c.item_key=i.item_key
            WHERE i.in_scope=1 AND c.collection_key=?
            """,
            (collection_key,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT
                i.item_key,
                i.title,
                i.date_added,
                COALESCE(i.source_attachment_key, '') AS source_attachment_key,
                i.mineru_state,
                i.qmd_state
            FROM items i
            JOIN item_collections c ON c.item_key=i.item_key
            WHERE
                i.in_scope=1
                AND c.collection_key=?
                AND i.mineru_state='parsed_current'
                AND i.qmd_state='indexed_current'
            ORDER BY i.date_added ASC, i.item_key ASC
            LIMIT ?
            """,
            (collection_key, limit),
        ).fetchall()

    direct_items = int(counts["direct_items"] or 0)
    reviewable_items = int(counts["reviewable_items"] or 0)
    return {
        "database": str(path),
        "last_sync_at": last_sync["value"] if last_sync else "",
        "collection": dict(root),
        "limit": limit,
        "direct_items": direct_items,
        "reviewable_items": reviewable_items,
        "blocked_items": direct_items - reviewable_items,
        "selected_items": len(rows),
        "items": [dict(row) for row in rows],
    }


def status_data(path: Path, item_key: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError(f"workflow database does not exist: {path}")
    with _connect(path) as connection:
        last_sync = connection.execute(
            "SELECT value FROM metadata WHERE key='last_sync_at'"
        ).fetchone()
        roots = [
            dict(row)
            for row in connection.execute(
                "SELECT collection_key, name, path FROM tracked_collections ORDER BY path"
            )
        ]
        health = [
            dict(row)
            for row in connection.execute(
                "SELECT system_name, status, detail_json, checked_at, error "
                "FROM system_health ORDER BY system_name"
            )
        ]
        for row in health:
            row["detail"] = json.loads(row.pop("detail_json"))
        if item_key:
            item = connection.execute(
                "SELECT * FROM items WHERE item_key=? AND in_scope=1", (item_key,)
            ).fetchone()
            if item is None:
                raise WorkflowError(f"item is not in the tracked scope: {item_key}")
            collections = [
                dict(row)
                for row in connection.execute(
                    "SELECT collection_key, collection_path FROM item_collections "
                    "WHERE item_key=? ORDER BY collection_path",
                    (item_key,),
                )
            ]
            attachments = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM pdf_attachments WHERE item_key=? ORDER BY role, attachment_key",
                    (item_key,),
                )
            ]
            mineru = connection.execute(
                "SELECT * FROM mineru_documents WHERE item_key=?", (item_key,)
            ).fetchone()
            qmd = connection.execute(
                "SELECT * FROM qmd_documents WHERE item_key=?", (item_key,)
            ).fetchone()
            return {
                "database": str(path),
                "last_sync_at": last_sync["value"] if last_sync else "",
                "item": dict(item),
                "collections": collections,
                "attachments": attachments,
                "mineru": dict(mineru) if mineru else None,
                "qmd": dict(qmd) if qmd else None,
            }

        counts: dict[str, Any] = {
            "scope_items": connection.execute(
                "SELECT COUNT(*) FROM items WHERE in_scope=1"
            ).fetchone()[0],
            "pdf_attachments": connection.execute(
                "SELECT COUNT(*) FROM pdf_attachments"
            ).fetchone()[0],
            "issues": connection.execute(
                "SELECT COUNT(*) FROM items WHERE in_scope=1 AND issue<>''"
            ).fetchone()[0],
        }
        for field in ("translation_state", "mineru_state", "qmd_state"):
            counts[field] = {
                row[0]: row[1]
                for row in connection.execute(
                    f"SELECT {field}, COUNT(*) FROM items WHERE in_scope=1 "
                    f"GROUP BY {field} ORDER BY {field}"
                )
            }
    return {
        "database": str(path),
        "last_sync_at": last_sync["value"] if last_sync else "",
        "tracked_collections": roots,
        "counts": counts,
        "system_health": health,
    }


def export_csv(path: Path, output: Path) -> int:
    if not path.is_file():
        raise WorkflowError(f"workflow database does not exist: {path}")
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with _connect(path) as connection:
            rows = connection.execute(
                """
                SELECT
                    i.item_key,
                    i.title,
                    i.date_added,
                    COALESCE((
                        SELECT group_concat(collection_path, ' | ')
                        FROM (
                            SELECT collection_path
                            FROM item_collections
                            WHERE item_key=i.item_key
                            ORDER BY collection_path
                        )
                    ), '') AS collection_paths,
                    COALESCE(i.source_attachment_key, '') AS source_attachment_key,
                    COALESCE((
                        SELECT title FROM pdf_attachments
                        WHERE attachment_key=i.source_attachment_key
                    ), '') AS source_attachment_title,
                    i.translation_state,
                    COALESCE((
                        SELECT attachment_key FROM pdf_attachments
                        WHERE item_key=i.item_key AND role='translated_pdf'
                        ORDER BY is_standard_title DESC, attachment_key
                        LIMIT 1
                    ), '') AS translation_attachment_key,
                    i.mineru_state,
                    COALESCE(m.parsed_attachment_key, '') AS parsed_attachment_key,
                    i.qmd_state,
                    COALESCE(q.document_ref, '') AS qmd_document_ref,
                    i.issue,
                    i.last_seen_at
                FROM items i
                LEFT JOIN mineru_documents m ON m.item_key=i.item_key
                LEFT JOIN qmd_documents q ON q.item_key=i.item_key
                WHERE i.in_scope=1
                ORDER BY i.title COLLATE NOCASE, i.item_key
                """
            ).fetchall()
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=EXPORT_FIELDS, lineterminator="\n"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        if os.name != "nt":
            os.chmod(output, 0o600)
        return len(rows)
    finally:
        temporary.unlink(missing_ok=True)


def format_status(data: dict[str, Any]) -> str:
    if "item" in data:
        item = data["item"]
        lines = [
            f"item_key: {item['item_key']}",
            f"title: {item['title']}",
            f"translation: {item['translation_state']}",
            f"mineru: {item['mineru_state']}",
            f"qmd: {item['qmd_state']}",
            f"issue: {item['issue'] or 'none'}",
            "collections:",
        ]
        lines.extend(f"- {row['collection_path']}" for row in data["collections"])
        lines.append("attachments:")
        for row in data["attachments"]:
            lines.append(
                f"- {row['attachment_key']} role={row['role']} title={row['title']}"
            )
        return "\n".join(lines)

    counts = data["counts"]
    lines = [
        f"database: {data['database']}",
        f"last_sync_at: {data['last_sync_at']}",
        f"scope_items: {counts['scope_items']}",
        f"pdf_attachments: {counts['pdf_attachments']}",
        f"issues: {counts['issues']}",
        "tracked_collections:",
    ]
    lines.extend(
        f"- {row['path']} ({row['collection_key']})"
        for row in data["tracked_collections"]
    )
    for field in ("translation_state", "mineru_state", "qmd_state"):
        lines.append(f"{field}:")
        lines.extend(f"- {key}: {value}" for key, value in counts[field].items())
    lines.append("system_health:")
    lines.extend(
        f"- {row['system_name']}: {row['status']}"
        + (f" ({row['error']})" if row["error"] else "")
        for row in data["system_health"]
    )
    return "\n".join(lines)


def format_next_batch(data: dict[str, Any]) -> str:
    collection = data["collection"]
    lines = [
        f"database: {data['database']}",
        f"last_sync_at: {data['last_sync_at']}",
        f"collection: {collection['path']} ({collection['collection_key']})",
        f"direct_items: {data['direct_items']}",
        f"reviewable_items: {data['reviewable_items']}",
        f"blocked_items: {data['blocked_items']}",
        f"selected_items: {data['selected_items']}",
        "batch:",
    ]
    for index, row in enumerate(data["items"], start=1):
        lines.extend(
            [
                f"{index}. {row['item_key']} {row['title']}",
                f"   date_added={row['date_added']}",
                f"   source_pdf={row['source_attachment_key']}",
                f"   mineru={row['mineru_state']} qmd={row['qmd_state']}",
            ]
        )
    return "\n".join(lines)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=default_database_path())
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Read live systems and update SQLite")
    sync.add_argument(
        "--collection",
        action="append",
        default=[],
        help="Exact collection key, globally unique name, or full path; repeatable",
    )

    status = subparsers.add_parser("status", help="Read status from SQLite")
    status.add_argument("--item", dest="item_key")
    status.add_argument("--json", action="store_true")

    next_batch = subparsers.add_parser(
        "next-batch", help="Select the next read-only collection review batch"
    )
    next_batch.add_argument(
        "--collection",
        required=True,
        help="Tracked collection key, exact name, or full path",
    )
    next_batch.add_argument("--limit", type=positive_int, default=5)
    next_batch.add_argument("--json", action="store_true")

    export = subparsers.add_parser("export-csv", help="Export one row per item")
    export.add_argument("--output", type=Path, default=default_export_path())

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "sync":
            collections = args.collection or tracked_collection_values(args.database)
            snapshot = build_snapshot(
                collections, source_attachment_hints(args.database)
            )
            store_snapshot(args.database, snapshot)
            print(
                json.dumps(
                    {
                        "database": str(args.database),
                        "last_sync_at": snapshot.observed_at,
                        "tracked_collections": [
                            root["path"] for root in snapshot.roots
                        ],
                        "scope_items": len(snapshot.items),
                        "pdf_attachments": len(snapshot.attachments),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "status":
            data = status_data(args.database, args.item_key)
            print(
                json.dumps(data, ensure_ascii=False, indent=2)
                if args.json
                else format_status(data)
            )
            return 0
        if args.command == "next-batch":
            data = next_batch_data(args.database, args.collection, args.limit)
            print(
                json.dumps(data, ensure_ascii=False, indent=2)
                if args.json
                else format_next_batch(data)
            )
            return 0
        if args.command == "export-csv":
            count = export_csv(args.database, args.output)
            print(
                json.dumps(
                    {"output": str(args.output), "rows": count}, ensure_ascii=False
                )
            )
            return 0
    except (
        OSError,
        sqlite3.DatabaseError,
        subprocess.SubprocessError,
        ValueError,
        requests.RequestException,
        zotero_collections.CollectionResolutionError,
        WorkflowError,
    ) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
