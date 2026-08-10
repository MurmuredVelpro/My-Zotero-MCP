#!/usr/bin/env python3
"""MCP server for local Zotero reads and guarded Web API writes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import (
    mineru_client,
    zotero_better_bibtex,
    zotero_collections,
    zotero_extract,
    zotero_local,
    zotero_translate,
    zotero_write,
)

SERVER_NAME = "zotero_mcp"
MCP_SERVER = Server(
    SERVER_NAME,
    instructions=(
        "Read the local Zotero library and use guarded plan/apply tools for writes. "
        "Only tools in the configured toolsets are listed or callable. Collection names "
        "and paths resolve exactly during read-only preflight; writes require exact keys."
    ),
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    structured = (
        {"ok": False, "error": text} if is_error else {"ok": True, "text": text}
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": is_error,
    }


def json_result(value: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": (
            {"ok": False, "error": text} if is_error else {"ok": True, "data": value}
        ),
        "isError": is_error,
    }


def structured_text_result(
    text: str, data: dict[str, Any], is_error: bool = False
) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": (
            {"ok": False, "error": text}
            if is_error
            else {"ok": True, "text": text, "data": data}
        ),
        "isError": is_error,
    }


def _resolve_reference_keys(
    references: Any,
    resolver: zotero_collections.CollectionResolver,
    *,
    label: str,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(references, list) or (not references and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise zotero_collections.CollectionResolutionError(
            f"{label} must be {qualifier} of collection references"
        )
    keys = [resolver.resolve(reference)["key"] for reference in references]
    return list(dict.fromkeys(keys))


def _resolve_paper_plan_items(
    records: Any,
) -> tuple[Any, zotero_collections.CollectionResolver | None]:
    if not isinstance(records, list) or not any(
        isinstance(record, dict) and "collections" in record for record in records
    ):
        return records, None
    for position, record in enumerate(records, start=1):
        if (
            isinstance(record, dict)
            and "collections" in record
            and "collection_keys" in record
        ):
            raise zotero_collections.CollectionResolutionError(
                f"item {position} cannot mix collection_keys with collections"
            )
    resolver = zotero_collections.CollectionResolver.from_zotero()
    prepared: list[Any] = []
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict) or "collections" not in record:
            prepared.append(record)
            continue
        updated = dict(record)
        updated["collection_keys"] = _resolve_reference_keys(
            updated.pop("collections"),
            resolver,
            label=f"item {position} collections",
        )
        prepared.append(updated)
    return prepared, resolver


def _resolve_reconcile_plan_items(
    records: Any,
) -> tuple[Any, zotero_collections.CollectionResolver | None]:
    reference_fields = {"add_collections", "remove_collections"}
    key_fields = {"add_collection_keys", "remove_collection_keys"}
    if not isinstance(records, list) or not any(
        isinstance(record, dict) and reference_fields.intersection(record)
        for record in records
    ):
        return records, None
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict) or not reference_fields.intersection(record):
            continue
        if key_fields.intersection(record):
            raise zotero_collections.CollectionResolutionError(
                f"item {position} cannot mix collection key fields "
                "with collection reference fields"
            )
        if not reference_fields.issubset(record):
            raise zotero_collections.CollectionResolutionError(
                f"item {position} requires add_collections and remove_collections"
            )
    resolver = zotero_collections.CollectionResolver.from_zotero()
    prepared: list[Any] = []
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict) or not reference_fields.intersection(record):
            prepared.append(record)
            continue
        updated = dict(record)
        updated["add_collection_keys"] = _resolve_reference_keys(
            updated.pop("add_collections"),
            resolver,
            label=f"item {position} add_collections",
            allow_empty=True,
        )
        updated["remove_collection_keys"] = _resolve_reference_keys(
            updated.pop("remove_collections"),
            resolver,
            label=f"item {position} remove_collections",
            allow_empty=True,
        )
        prepared.append(updated)
    return prepared, resolver


def _resolve_single_collection_argument(
    arguments: dict[str, Any],
    *,
    key_field: str,
    reference_field: str,
) -> tuple[str | None, zotero_collections.CollectionResolver | None]:
    has_key = key_field in arguments
    has_reference = reference_field in arguments
    if has_key and has_reference:
        raise zotero_collections.CollectionResolutionError(
            f"cannot mix {key_field} with {reference_field}"
        )
    if has_reference:
        resolver = zotero_collections.CollectionResolver.from_zotero()
        return resolver.resolve(arguments[reference_field])["key"], resolver
    return arguments.get(key_field), None


def tool_ping(_: dict[str, Any]) -> dict[str, Any]:
    status = zotero_local.ping_status()
    data = dict(status)
    sample_summary = None
    if "sample_item" in data:
        sample_summary = zotero_local.item_summary(data["sample_item"])
        data["sample_item"] = sample_summary
    return structured_text_result(
        zotero_local.format_ping_status(status, sample_summary), data
    )


def tool_search(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not query:
        return text_result("Missing required argument: query", True)
    limit = int(arguments.get("limit", 5))
    item_type = arguments.get("item_type", "-attachment")
    items = zotero_local.search_items(query, limit, item_type)
    summaries = [zotero_local.item_summary(item) for item in items]
    return structured_text_result(
        zotero_local.format_item_summaries(summaries, "No matching Zotero items."),
        {
            "query": query,
            "count": len(summaries),
            "items": summaries,
        },
    )


def tool_match(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not query:
        return text_result("Missing required argument: query", True)
    field = arguments.get("field", "title")
    limit = int(arguments.get("limit", 20))
    scan_limit = int(arguments.get("scan_limit", 1000))
    threshold = float(arguments.get("threshold", 0.85))
    best = bool(arguments.get("best", True))
    result = zotero_local.match_items(
        query,
        field=field,
        limit=limit,
        scan_limit=scan_limit,
        threshold=threshold,
        best=best,
    )
    data = zotero_local.summarize_match_result(result)
    return structured_text_result(zotero_local.format_match_summaries(data), data)


def tool_item(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    output_format = arguments.get("format", "markdown")
    if output_format in {"json", "bibtex"}:
        item = zotero_local.get_item(key)
        if output_format == "json":
            return json_result(item)
        try:
            exported = zotero_better_bibtex.export_bibtex(key)
        except zotero_better_bibtex.BetterBibTeXError as exc:
            return text_result(str(exc), True)
        return structured_text_result(
            exported.bibtex,
            {
                "item_key": exported.item_key,
                "citation_key": exported.citation_key,
                "format": "bibtex",
                "source": exported.source,
                "bibtex": exported.bibtex,
            },
        )
    item = zotero_local.get_item(key)
    summary = zotero_local.item_summary(item)
    return structured_text_result(zotero_local.format_item_summary(summary), summary)


def tool_children(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    children = zotero_local.get_children(key)
    summaries = [zotero_local.item_summary(child) for child in children]
    return structured_text_result(
        zotero_local.format_item_summaries(summaries, "No child items."),
        {
            "item_key": key,
            "count": len(summaries),
            "children": summaries,
        },
    )


def tool_annotations(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    records = zotero_local.item_annotations(key, int(arguments.get("limit", 100)))
    return json_result({"item_key": key, "count": len(records), "annotations": records})


def tool_notes(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    records = zotero_local.item_notes(key, int(arguments.get("limit", 20)))
    return json_result({"item_key": key, "count": len(records), "notes": records})


def tool_citation_key(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    item = zotero_local.zotero_get(f"users/0/items/{key}")
    try:
        citekey = zotero_better_bibtex.citation_key(key)
    except zotero_better_bibtex.BetterBibTeXError as exc:
        citekey = zotero_better_bibtex.citation_key_from_item(item)
        if not citekey:
            return text_result(str(exc), True)
        source = "zotero_item"
    else:
        source = "better_bibtex"
    return json_result({"item_key": key, "citation_key": citekey, "source": source})


def tool_collections(_: dict[str, Any]) -> dict[str, Any]:
    collections = zotero_local.list_collections()
    return structured_text_result(
        zotero_local.format_collections(collections),
        {"count": len(collections), "collections": collections},
    )


def tool_resolve_collection(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return json_result(zotero_collections.resolve_collection(arguments))
    except zotero_collections.CollectionResolutionError as exc:
        return text_result(str(exc), True)


def tool_item_collections(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    result = zotero_local.get_item_collections(key)
    return structured_text_result(zotero_local.format_item_collections(result), result)


def tool_collection_items(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        key, resolver = _resolve_single_collection_argument(
            arguments,
            key_field="key",
            reference_field="collection",
        )
        if not key:
            return text_result("Missing required argument: key or collection", True)
        recursive = bool(arguments.get("recursive", False))
        limit = int(arguments.get("limit", 100))
        if resolver is None:
            result = zotero_local.list_collection_items(
                key, recursive=recursive, limit=limit
            )
        else:
            result = zotero_local.list_collection_items(
                key,
                recursive=recursive,
                limit=limit,
                collections=resolver.collections,
            )
        return structured_text_result(
            zotero_local.format_collection_items(result), result
        )
    except zotero_collections.CollectionResolutionError as exc:
        return text_result(str(exc), True)


def tool_web_api_status(_: dict[str, Any]) -> dict[str, Any]:
    try:
        return json_result(zotero_write.web_api_status())
    except zotero_write.ZoteroWriteError as exc:
        return text_result(str(exc), True)


def tool_plan_paper_import(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        items, resolver = _resolve_paper_plan_items(arguments.get("items"))
        if resolver is None:
            result = zotero_write.plan_paper_import(items)
        else:
            result = zotero_write.plan_paper_import(
                items,
                collection_keys=resolver.collection_keys,
            )
        return json_result(result)
    except (
        zotero_collections.CollectionResolutionError,
        zotero_write.ZoteroWriteError,
    ) as exc:
        return text_result(str(exc), True)


def tool_apply_paper_import(arguments: dict[str, Any]) -> dict[str, Any]:
    items = arguments.get("items")
    confirm = arguments.get("confirm") is True
    try:
        return json_result(zotero_write.execute_paper_import(items, confirm))
    except zotero_write.ZoteroWriteError as exc:
        return text_result(str(exc), True)


def tool_plan_collection_reconcile(arguments: dict[str, Any]) -> dict[str, Any]:
    allow_no_collections = arguments.get("allow_no_collections") is True
    try:
        items, resolver = _resolve_reconcile_plan_items(arguments.get("items"))
        kwargs: dict[str, Any] = {
            "allow_no_collections": allow_no_collections,
        }
        if resolver is not None:
            kwargs["collection_keys"] = resolver.collection_keys
        return json_result(
            zotero_write.plan_collection_reconcile(
                items,
                **kwargs,
            )
        )
    except (
        zotero_collections.CollectionResolutionError,
        zotero_write.ZoteroWriteError,
    ) as exc:
        return text_result(str(exc), True)


def tool_apply_collection_reconcile(arguments: dict[str, Any]) -> dict[str, Any]:
    items = arguments.get("items")
    confirm = arguments.get("confirm") is True
    allow_no_collections = arguments.get("allow_no_collections") is True
    try:
        return json_result(
            zotero_write.execute_collection_reconcile(
                items,
                confirm,
                allow_no_collections=allow_no_collections,
            )
        )
    except zotero_write.ZoteroWriteError as exc:
        return text_result(str(exc), True)


def tool_plan_pdf_attachment_delete(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        collection_key, resolver = _resolve_single_collection_argument(
            arguments,
            key_field="collection_key",
            reference_field="collection",
        )
        recursive = bool(arguments.get("recursive", False))
        limit = int(arguments.get("limit", 1000))
        offset = int(arguments.get("offset", 0))
        page_size = int(arguments.get("page_size", 50))
        kwargs: dict[str, Any] = {
            "recursive": recursive,
            "limit": limit,
            "offset": offset,
            "page_size": page_size,
        }
        if resolver is not None:
            kwargs["collections"] = resolver.collections
        return json_result(
            zotero_write.plan_pdf_attachment_delete(
                collection_key,
                **kwargs,
            )
        )
    except (
        zotero_collections.CollectionResolutionError,
        zotero_write.ZoteroWriteError,
    ) as exc:
        return text_result(str(exc), True)


def tool_apply_pdf_attachment_delete(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return json_result(
            zotero_write.execute_pdf_attachment_delete(
                arguments.get("items"),
                arguments.get("collection_key"),
                arguments.get("backup_dir"),
                arguments.get("confirm") is True,
                recursive=bool(arguments.get("recursive", False)),
                allow_shared_parents=arguments.get("allow_shared_parents") is True,
                allow_annotations=arguments.get("allow_annotations") is True,
            )
        )
    except zotero_write.ZoteroWriteError as exc:
        return text_result(str(exc), True)


def tool_plan_manual_translation_rename(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        return json_result(
            zotero_translate.plan_manual_translation_renames(arguments.get("item_keys"))
        )
    except (
        zotero_translate.TranslationError,
        zotero_write.ZoteroWriteError,
    ) as exc:
        return text_result(str(exc), True)


def tool_apply_manual_translation_rename(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        return json_result(
            zotero_translate.apply_manual_translation_renames(
                arguments.get("items"),
                arguments.get("confirm") is True,
            )
        )
    except (
        zotero_translate.TranslationError,
        zotero_write.ZoteroWriteError,
    ) as exc:
        return text_result(str(exc), True)


def tool_extract_text(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    try:
        item = zotero_local.zotero_get(f"users/0/items/{key}")
        document = zotero_extract.extract_document(
            item,
            attachment_priority=arguments.get(
                "attachment_priority", "mineru_then_local"
            ),
            out_dir=arguments.get("out_dir"),
            output=arguments.get("output"),
        )
    except (SystemExit, zotero_extract.ExtractionError) as exc:
        return text_result(str(exc), True)

    data = {
        "item_key": document.item_key,
        "attachment_key": document.attachment_key,
        "source": document.source,
        "source_path": str(document.source_path),
        "text_path": str(document.output_path),
        "text_length": len(document.text),
    }
    return structured_text_result(
        "\n".join(
            [
                f"source: {document.source}",
                f"attachment_key: {document.attachment_key}",
                f"source_path: {document.source_path}",
                f"text: {document.output_path}",
            ]
        ),
        data,
    )


def tool_mineru_submit(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    if not key:
        return text_result("Missing required argument: key", True)
    if arguments.get("confirm") is not True:
        return text_result(
            "MinerU upload requires explicit confirm=true approval.", True
        )
    item = zotero_local.zotero_get(f"users/0/items/{key}")
    item_key = item.get("data", {}).get("key") or key
    attachment = zotero_local.english_pdf_attachment_for_item(item)
    existing = mineru_client.find_local_result(item_key)
    if existing:
        try:
            workflow = zotero_extract.load_mineru_workflow()
        except zotero_extract.MinerUWorkflowUnavailable as exc:
            return text_result(str(exc), True)
        status = workflow.tracked_result_status(item_key, str(attachment["key"]))
        if status == "stale":
            tracked = workflow.todo_rows_by_key().get(item_key, {})
            return text_result(
                "Existing MinerU result is stale: "
                f"parsed_attachment_key={tracked.get('parsed_attachment_key', '')}, "
                f"current_attachment_key={attachment['key']}. "
                "Use zotero-mineru to replace it safely.",
                True,
            )
        details = mineru_client.artifact_summary(Path(existing["output_dir"]))
        details.update(
            {
                "already_parsed": True,
                "data_id": item_key,
                "state": "done",
            }
        )
        return json_result(details)
    pdf = Path(attachment["path"])
    result = mineru_client.submit_file(
        pdf,
        data_id=item_key,
        model_version="vlm",
        language=arguments.get("language", "en"),
        enable_formula=bool(arguments.get("enable_formula", True)),
        enable_table=bool(arguments.get("enable_table", True)),
        is_ocr=bool(arguments.get("is_ocr", False)),
        page_ranges=arguments.get("page_ranges"),
    )
    result["pdf"] = str(pdf)
    result["attachment_key"] = str(attachment["key"])
    result["default_output_root"] = str(mineru_client.DEFAULT_OUTPUT_ROOT)
    result["next_action"] = (
        "Poll and download this item with zotero_mineru_result. Use "
        "zotero-mineru for recoverable collection batches."
    )
    return json_result(result)


def tool_mineru_result(arguments: dict[str, Any]) -> dict[str, Any]:
    batch_id = arguments.get("batch_id")
    if not batch_id:
        return text_result("Missing required argument: batch_id", True)
    batch = mineru_client.get_batch(batch_id)
    result = mineru_client.first_extract_result(batch)
    state = result.get("state", "unknown")
    response: dict[str, Any] = {
        "batch_id": batch_id,
        "data_id": result.get("data_id"),
        "file_name": result.get("file_name"),
        "state": state,
    }
    if result.get("extract_progress"):
        response["extract_progress"] = result["extract_progress"]
    if state == "failed":
        response["err_msg"] = result.get("err_msg")
        return json_result(response, True)
    if state != "done":
        response["next_action"] = "Poll this batch again after 30-60 seconds."
        return json_result(response)

    full_zip_url = result.get("full_zip_url")
    if not full_zip_url:
        return text_result("MinerU task is done but full_zip_url is missing", True)
    out_dir = arguments.get("out_dir")
    if out_dir:
        destination = Path(out_dir)
    else:
        data_id = result.get("data_id")
        try:
            item = (
                zotero_local.zotero_get(f"users/0/items/{data_id}") if data_id else {}
            )
        except Exception:  # noqa: BLE001 - optional lookup falls back to a stable item shape
            item = {"data": {"key": data_id or "zotero"}}
        destination = mineru_client.default_result_dir(item)
    response.update(mineru_client.download_and_extract(full_zip_url, destination))
    response["next_action"] = "Verify the downloaded artifacts."
    return json_result(response)


def tool_render_pages(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    pages = arguments.get("pages")
    if not key:
        return text_result("Missing required argument: key", True)
    if not pages:
        return text_result("Missing required argument: pages", True)
    out_dir = arguments.get("out_dir")
    if not out_dir:
        return text_result("Missing required argument: out_dir", True)
    result = zotero_local.render_pages(
        key,
        pages,
        out_dir,
        dpi=int(arguments.get("dpi", 180)),
        image_format=arguments.get("format", "png"),
    )
    return structured_text_result(zotero_local.format_render_pages(result), result)


def tool_find_figure_pages(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key")
    figure = arguments.get("figure")
    if not key:
        return text_result("Missing required argument: key", True)
    if not figure:
        return text_result("Missing required argument: figure", True)
    result = zotero_local.find_figure_pages(
        key,
        figure,
        limit=int(arguments.get("limit", 10)),
        context_chars=int(arguments.get("context", 180)),
    )
    return structured_text_result(zotero_local.format_figure_pages(result), result)


COLLECTION_REFERENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "pattern": "^[A-Z0-9]{8}$",
            "description": "Exact Zotero collection key.",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Exact globally unique Zotero collection name.",
        },
        "path": {
            "type": "string",
            "minLength": 1,
            "description": "Exact full path such as Projects > Glioma.",
        },
    },
    "oneOf": [
        {"required": ["key"]},
        {"required": ["name"]},
        {"required": ["path"]},
    ],
    "additionalProperties": False,
}


COLLECTION_KEY_LIST_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "pattern": "^[A-Z0-9]{8}$"},
    "minItems": 1,
    "maxItems": 20,
    "uniqueItems": True,
    "description": "Target Zotero collection keys. Membership is added as a union.",
}


COLLECTION_REFERENCE_LIST_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": COLLECTION_REFERENCE_SCHEMA,
    "minItems": 1,
    "maxItems": 20,
    "description": "Exact collection references resolved to keys during read-only preflight.",
}


PAPER_METADATA_PROPERTIES: dict[str, Any] = {
    "source_id": {
        "type": "string",
        "description": "Optional stable row/source label used only in progress output.",
    },
    "title": {"type": "string", "minLength": 1},
    "doi": {"type": "string"},
    "pmid": {"type": "string"},
    "pmcid": {"type": "string"},
    "arxiv": {"type": "string"},
}


PAPER_IMPORT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **PAPER_METADATA_PROPERTIES,
        "collection_keys": COLLECTION_KEY_LIST_SCHEMA,
    },
    "required": ["title", "collection_keys"],
    "additionalProperties": False,
}


PAPER_IMPORT_PLAN_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **PAPER_METADATA_PROPERTIES,
        "collection_keys": COLLECTION_KEY_LIST_SCHEMA,
        "collections": COLLECTION_REFERENCE_LIST_SCHEMA,
    },
    "required": ["title"],
    "oneOf": [
        {
            "required": ["collection_keys"],
            "not": {"required": ["collections"]},
        },
        {
            "required": ["collections"],
            "not": {"required": ["collection_keys"]},
        },
    ],
    "additionalProperties": False,
}


COLLECTION_RECONCILE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "item_key": {
            "type": "string",
            "pattern": "^[A-Z0-9]{8}$",
            "description": "Top-level Zotero paper item key.",
        },
        "add_collection_keys": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[A-Z0-9]{8}$"},
            "maxItems": 20,
            "uniqueItems": True,
            "description": "Collection memberships to add.",
        },
        "remove_collection_keys": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[A-Z0-9]{8}$"},
            "maxItems": 20,
            "uniqueItems": True,
            "description": (
                "Collection memberships to remove. This never deletes or trashes the item."
            ),
        },
    },
    "required": ["item_key", "add_collection_keys", "remove_collection_keys"],
    "additionalProperties": False,
}


COLLECTION_RECONCILE_PLAN_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **COLLECTION_RECONCILE_ITEM_SCHEMA["properties"],
        "add_collections": {
            "type": "array",
            "items": COLLECTION_REFERENCE_SCHEMA,
            "maxItems": 20,
            "description": "Exact collection references to resolve and add.",
        },
        "remove_collections": {
            "type": "array",
            "items": COLLECTION_REFERENCE_SCHEMA,
            "maxItems": 20,
            "description": "Exact collection references to resolve and remove.",
        },
    },
    "required": ["item_key"],
    "oneOf": [
        {
            "required": ["add_collection_keys", "remove_collection_keys"],
            "not": {
                "anyOf": [
                    {"required": ["add_collections"]},
                    {"required": ["remove_collections"]},
                ]
            },
        },
        {
            "required": ["add_collections", "remove_collections"],
            "not": {
                "anyOf": [
                    {"required": ["add_collection_keys"]},
                    {"required": ["remove_collection_keys"]},
                ]
            },
        },
    ],
    "additionalProperties": False,
}


PDF_ATTACHMENT_DELETE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "parent_item_key": {
            "type": "string",
            "pattern": "^[A-Z0-9]{8}$",
            "description": "Top-level Zotero paper item key returned by the deletion plan.",
        },
        "attachment_key": {
            "type": "string",
            "pattern": "^[A-Z0-9]{8}$",
            "description": "Exact PDF child attachment key returned by the deletion plan.",
        },
    },
    "required": ["parent_item_key", "attachment_key"],
    "additionalProperties": False,
}


TRANSLATION_RENAME_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "parent_item_key": {
            "type": "string",
            "pattern": "^[A-Z0-9]{8}$",
        },
        "source_attachment_key": {
            "type": "string",
            "pattern": "^[A-Z0-9]{8}$",
        },
        "translation_attachment_key": {
            "type": "string",
            "pattern": "^[A-Z0-9]{8}$",
        },
    },
    "required": [
        "parent_item_key",
        "source_attachment_key",
        "translation_attachment_key",
    ],
    "additionalProperties": False,
}


TOOL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {},
        "text": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": False,
}


TOOLS: dict[str, dict[str, Any]] = {
    "zotero_ping": {
        "description": "Test access to the configured Zotero Local API from the current environment.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_ping,
    },
    "zotero_search": {
        "description": "Search Zotero items and resolve local PDF attachment paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Title, author, DOI, or keyword.",
                },
                "limit": {"type": "integer", "default": 5},
                "item_type": {"type": "string", "default": "-attachment"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": tool_search,
    },
    "zotero_match": {
        "description": "Find the best Zotero item by exact or near-exact title/DOI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Exact or near-exact title/DOI.",
                },
                "field": {
                    "type": "string",
                    "enum": ["title", "doi"],
                    "default": "title",
                },
                "limit": {"type": "integer", "default": 20},
                "scan_limit": {"type": "integer", "default": 1000},
                "threshold": {"type": "number", "default": 0.85},
                "best": {"type": "boolean", "default": True},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": tool_match,
    },
    "zotero_item": {
        "description": (
            "Show one Zotero item as readable metadata, complete JSON, or a Better "
            "BibTeX export. Markdown remains the default and includes local PDF paths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "format": {
                    "type": "string",
                    "enum": ["markdown", "json", "bibtex"],
                    "default": "markdown",
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_item,
    },
    "zotero_children": {
        "description": "List child attachments/notes for one Zotero parent item key.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_children,
    },
    "zotero_get_annotations": {
        "description": (
            "Return normalized Zotero PDF annotations for one parent paper or attachment "
            "key, including page, text, comment, color, tags, and attachment context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_annotations,
    },
    "zotero_get_notes": {
        "description": (
            "Return child notes for one Zotero item key as both original HTML and "
            "normalized plain text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 20,
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_notes,
    },
    "zotero_get_citation_key": {
        "description": (
            "Resolve the Better BibTeX citation key for one exact Zotero item key. "
            "Uses the running Better BibTeX plugin first, then its stored Zotero "
            "citationKey/Extra field when available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_citation_key,
    },
    "zotero_collections": {
        "description": "List Zotero collections with full nested paths, keys, and direct item/subcollection counts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_collections,
    },
    "zotero_resolve_collection": {
        "description": (
            "Resolve one exact Zotero collection key, globally unique name, or full path. "
            "Fails closed on missing, duplicate, deleted, or malformed collections. Similar "
            "candidates are suggestions only and are never selected automatically."
        ),
        "inputSchema": COLLECTION_REFERENCE_SCHEMA,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": tool_resolve_collection,
    },
    "zotero_item_collections": {
        "description": "Show every Zotero collection containing an item. Child attachment/note keys are resolved to their top-level parent item.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_item_collections,
    },
    "zotero_collection_items": {
        "description": (
            "List items assigned to an exact Zotero collection key or collection reference, "
            "optionally including nested subcollections with duplicate items removed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Collection key from zotero_collections.",
                },
                "collection": COLLECTION_REFERENCE_SCHEMA,
                "recursive": {"type": "boolean", "default": False},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 100,
                },
            },
            "oneOf": [
                {"required": ["key"], "not": {"required": ["collection"]}},
                {"required": ["collection"], "not": {"required": ["key"]}},
            ],
            "additionalProperties": False,
        },
        "handler": tool_collection_items,
    },
    "zotero_web_api_status": {
        "description": (
            "Read-only check for official Zotero Web API credentials, expected user ID, "
            "personal-library write permission, and file-write permission. Performs no write."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "handler": tool_web_api_status,
    },
    "zotero_plan_paper_import": {
        "description": (
            "Read-only preflight for up to 50 papers. Exact DOI, PMID, PMCID, and arXiv IDs "
            "may match existing items. A title-only match is always ambiguous. Reports create, "
            "collection-union, unchanged, manual, invalid, or ambiguous actions. Never touches PDFs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": PAPER_IMPORT_PLAN_ITEM_SCHEMA,
                    "minItems": 1,
                    "maxItems": 50,
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": tool_plan_paper_import,
    },
    "zotero_apply_paper_import": {
        "description": (
            "Apply a preflighted batch of up to 50 papers through the official Zotero Web API. "
            "Rescans the local library, performs fail-closed cloud exact-ID checks before creation, "
            "resolves DOI/PMID/arXiv metadata, and versions collection unions for existing items. "
            "Never removes memberships, merges items, edits existing metadata, or creates/downloads/"
            "uploads PDF attachments. Zotero desktop sync makes cloud writes visible locally. "
            "Requires confirm=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": PAPER_IMPORT_ITEM_SCHEMA,
                    "minItems": 1,
                    "maxItems": 50,
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to authorize Zotero writes.",
                },
            },
            "required": ["items", "confirm"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "handler": tool_apply_paper_import,
    },
    "zotero_plan_collection_reconcile": {
        "description": (
            "Read-only preflight for adding and removing collection memberships on up to 50 "
            "existing top-level Zotero papers. Computes current union-add-remove targets while "
            "preserving every unmentioned membership, including collections outside the reviewed "
            "tree. Never edits metadata, attachments, notes, tags, or item deletion state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": COLLECTION_RECONCILE_PLAN_ITEM_SCHEMA,
                    "minItems": 1,
                    "maxItems": 50,
                },
                "allow_no_collections": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Permit an item to end with no collection memberships. Keep false unless "
                        "the user explicitly approves that result."
                    ),
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": tool_plan_collection_reconcile,
    },
    "zotero_apply_collection_reconcile": {
        "description": (
            "Apply an explicitly reviewed collection-membership plan for up to 50 existing "
            "top-level Zotero papers through the official Web API. Uses one versioned PATCH per "
            "changed item, recomputes after 409/412 conflicts, preserves every unmentioned "
            "membership, and verifies additions/removals by readback. Removing membership only "
            "unfiles the item from that collection: this tool never calls item DELETE, moves an "
            "item to trash, or edits/deletes PDFs, notes, tags, or metadata. Requires confirm=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": COLLECTION_RECONCILE_ITEM_SCHEMA,
                    "minItems": 1,
                    "maxItems": 50,
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to authorize collection membership writes.",
                },
                "allow_no_collections": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Permit an item to end with no collection memberships. Keep false unless "
                        "the user explicitly approves that result."
                    ),
                },
            },
            "required": ["items", "confirm"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "handler": tool_apply_collection_reconcile,
    },
    "zotero_plan_pdf_attachment_delete": {
        "description": (
            "Read-only scan of PDF child attachments in one Zotero collection. Reports exact "
            "parent and attachment keys, local size, link mode, shared collection memberships, "
            "annotations/notes, blockers, and reclaimable bytes. Performs no write."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection_key": {
                    "type": "string",
                    "pattern": "^[A-Z0-9]{8}$",
                },
                "collection": COLLECTION_REFERENCE_SCHEMA,
                "recursive": {"type": "boolean", "default": False},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 1000,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 50,
                },
            },
            "oneOf": [
                {
                    "required": ["collection_key"],
                    "not": {"required": ["collection"]},
                },
                {
                    "required": ["collection"],
                    "not": {"required": ["collection_key"]},
                },
            ],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": tool_plan_pdf_attachment_delete,
    },
    "zotero_apply_pdf_attachment_delete": {
        "description": (
            "Permanently delete up to 50 explicitly reviewed PDF child attachments through the "
            "official Zotero Web API while preserving parent paper items. Rescans the collection, "
            "fails closed on shared parents or annotations by default, verifies local/cloud state, "
            "creates and SHA-256-verifies a backup outside Zotero storage before any DELETE, uses "
            "If-Unmodified-Since-Version, and stops on conflicts or unknown state. Requires "
            "confirm=true. Zotero desktop sync is required for local file removal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection_key": {
                    "type": "string",
                    "pattern": "^[A-Z0-9]{8}$",
                },
                "items": {
                    "type": "array",
                    "items": PDF_ATTACHMENT_DELETE_ITEM_SCHEMA,
                    "minItems": 1,
                    "maxItems": 50,
                },
                "backup_dir": {
                    "type": "string",
                    "description": "Absolute backup root outside Zotero storage.",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to authorize permanent PDF attachment deletion.",
                },
                "recursive": {"type": "boolean", "default": False},
                "allow_shared_parents": {"type": "boolean", "default": False},
                "allow_annotations": {"type": "boolean", "default": False},
            },
            "required": ["collection_key", "items", "backup_dir", "confirm"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        "handler": tool_apply_pdf_attachment_delete,
    },
    "zotero_plan_manual_translation_rename": {
        "description": (
            "Read-only preflight for PDF2zh attachments created through Zotero's manual "
            "translation command. Resolves each exact paper or child key to its top-level "
            "paper, identifies one English source PDF and one translated PDF, checks local/cloud "
            "agreement and filename conflicts, and proposes title CN plus an English-source "
            "filename ending in 的全文翻译.pdf. Performs no write."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_keys": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[A-Z0-9]{8}$"},
                    "minItems": 1,
                    "maxItems": 50,
                }
            },
            "required": ["item_keys"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "handler": tool_plan_manual_translation_rename,
    },
    "zotero_apply_manual_translation_rename": {
        "description": (
            "Apply an explicitly reviewed manual-translation rename plan through versioned "
            "Zotero Web API PATCH requests. Changes only translated attachment title and "
            "filename, verifies cloud readback, and relies on normal Zotero sync to rename the "
            "local stored file. Never changes PDF content, parent metadata, annotations, or "
            "attachment keys. Requires confirm=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": TRANSLATION_RENAME_ITEM_SCHEMA,
                    "minItems": 1,
                    "maxItems": 50,
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to authorize attachment metadata writes.",
                },
            },
            "required": ["items", "confirm"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "handler": tool_apply_manual_translation_rename,
    },
    "zotero_extract_text": {
        "description": (
            "Materialize full text for one confidently English Zotero PDF. By default "
            "uses a current MinerU full.md first, then falls back to local pdftotext; "
            "stale MinerU results are never reused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "out_dir": {"type": "string"},
                "output": {"type": "string"},
                "attachment_priority": {
                    "type": "string",
                    "enum": ["mineru_then_local", "mineru_only", "local_only"],
                    "default": "mineru_then_local",
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_extract_text,
    },
    "zotero_mineru_submit": {
        "description": "Ad hoc single-item submission for the preferred/default PDF parser. Reuse an existing local MinerU result when present; otherwise select one confidently English Zotero PDF, submit it to MinerU's cloud precision API, and return a batch_id. Chinese translations are never submitted. Use zotero-mineru for recoverable collection batches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Zotero parent item key."},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true after explicit user approval to upload the PDF to MinerU.",
                },
                "language": {"type": "string", "default": "en"},
                "enable_formula": {"type": "boolean", "default": True},
                "enable_table": {"type": "boolean", "default": True},
                "is_ocr": {"type": "boolean", "default": False},
                "page_ranges": {
                    "type": "string",
                    "description": "Optional MinerU page range, e.g. 1-20 or 2,4-6.",
                },
            },
            "required": ["key", "confirm"],
            "additionalProperties": False,
        },
        "handler": tool_mineru_submit,
    },
    "zotero_mineru_result": {
        "description": "Poll one ad hoc MinerU precision API batch; when done, download and safely extract canonical artifacts under the configured MinerU output directory, or under an explicit out_dir.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string"},
                "out_dir": {
                    "type": "string",
                    "description": "Optional final extraction directory.",
                },
            },
            "required": ["batch_id"],
            "additionalProperties": False,
        },
        "handler": tool_mineru_result,
    },
    "zotero_render_pages": {
        "description": "Render selected PDF pages as images for visual inspection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "pages": {
                    "type": "string",
                    "description": "1-based pages, e.g. 3 or 3-5,8.",
                },
                "out_dir": {"type": "string"},
                "dpi": {"type": "integer", "default": 180},
                "format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
            },
            "required": ["key", "pages", "out_dir"],
            "additionalProperties": False,
        },
        "handler": tool_render_pages,
    },
    "zotero_find_figure_pages": {
        "description": "Find PDF pages that mention a figure label, useful before rendering pages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "figure": {
                    "type": "string",
                    "description": "Figure label, e.g. 'Fig. 2' or 'Extended Data Fig. 1'.",
                },
                "limit": {"type": "integer", "default": 10},
                "context": {"type": "integer", "default": 180},
            },
            "required": ["key", "figure"],
            "additionalProperties": False,
        },
        "handler": tool_find_figure_pages,
    },
}


DEFAULT_READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


TOOL_ANNOTATION_OVERRIDES = {
    "zotero_extract_text": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "zotero_mineru_submit": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "zotero_mineru_result": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "zotero_render_pages": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
}


TOOLSETS: dict[str, tuple[str, ...]] = {
    "literature": (
        "zotero_ping",
        "zotero_search",
        "zotero_match",
        "zotero_item",
        "zotero_children",
        "zotero_get_citation_key",
        "zotero_collections",
        "zotero_resolve_collection",
        "zotero_item_collections",
        "zotero_collection_items",
        "zotero_web_api_status",
        "zotero_plan_paper_import",
        "zotero_apply_paper_import",
    ),
    "review": (
        "zotero_item",
        "zotero_children",
        "zotero_get_annotations",
        "zotero_get_notes",
        "zotero_extract_text",
        "zotero_mineru_submit",
        "zotero_mineru_result",
        "zotero_render_pages",
        "zotero_find_figure_pages",
    ),
    "maintenance": (
        "zotero_collections",
        "zotero_resolve_collection",
        "zotero_item_collections",
        "zotero_collection_items",
        "zotero_web_api_status",
        "zotero_plan_collection_reconcile",
        "zotero_apply_collection_reconcile",
        "zotero_plan_pdf_attachment_delete",
        "zotero_apply_pdf_attachment_delete",
        "zotero_plan_manual_translation_rename",
        "zotero_apply_manual_translation_rename",
    ),
}


def load_local_workflows() -> None:
    if os.environ.get("ZOTERO_MCP_DISABLE_PRIVATE", "").strip() == "1":
        return
    try:
        import zotero_mcp_private
    except ModuleNotFoundError as exc:
        if exc.name == "zotero_mcp_private":
            return
        raise
    zotero_mcp_private.register(TOOLS, TOOLSETS, json_result, text_result)


load_local_workflows()
DEFAULT_TOOLSETS = ("literature",)
ACTIVE_TOOLSETS = DEFAULT_TOOLSETS


def parse_toolsets(value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    raw = value.split(",") if isinstance(value, str) else list(value)
    selected = tuple(dict.fromkeys(part.strip() for part in raw if part.strip()))
    if not selected:
        raise ValueError("toolsets must contain at least one name")
    unknown = sorted(set(selected) - (set(TOOLSETS) | {"all"}))
    if unknown:
        allowed = ", ".join((*TOOLSETS, "all"))
        raise ValueError(f"unknown toolsets: {unknown}; choose from: {allowed}")
    return selected


def configure_toolsets(value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    global ACTIVE_TOOLSETS
    ACTIVE_TOOLSETS = parse_toolsets(value)
    return ACTIVE_TOOLSETS


def selected_tool_names(
    toolsets: str | tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    selected = ACTIVE_TOOLSETS if toolsets is None else parse_toolsets(toolsets)
    if "all" in selected:
        return tuple(TOOLS)
    visible = {name for toolset in selected for name in TOOLSETS[toolset]}
    return tuple(name for name in TOOLS if name in visible)


def tool_definitions(
    toolsets: str | tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    definitions = []
    for name in selected_tool_names(toolsets):
        spec = TOOLS[name]
        definition = {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
            "outputSchema": TOOL_OUTPUT_SCHEMA,
            "annotations": spec.get(
                "annotations",
                TOOL_ANNOTATION_OVERRIDES.get(name, DEFAULT_READ_ONLY_ANNOTATIONS),
            ),
        }
        definitions.append(definition)
    return definitions


@MCP_SERVER.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return [mcp_types.Tool.model_validate(tool) for tool in tool_definitions()]


@MCP_SERVER.call_tool(validate_input=True)
async def call_tool(
    name: str, arguments: dict[str, Any]
) -> tuple[list[mcp_types.TextContent], dict[str, Any]] | mcp_types.CallToolResult:
    spec = TOOLS.get(name) if name in selected_tool_names() else None
    if spec is None:
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=f"Unknown tool: {name}")],
            structuredContent={"ok": False, "error": f"Unknown tool: {name}"},
            isError=True,
        )

    try:
        raw_result = await anyio.to_thread.run_sync(spec["handler"], arguments)
    except SystemExit as exc:
        raw_result = text_result(str(exc), True)
    except Exception as exc:  # noqa: BLE001 - MCP boundary must return a structured error
        print(
            f"Unhandled {name} error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raw_result = text_result(
            f"Unexpected server error ({type(exc).__name__}). Check MCP server stderr.",
            True,
        )

    result = mcp_types.CallToolResult.model_validate(raw_result)
    if result.isError:
        return result
    return (
        [
            content
            for content in result.content
            if isinstance(content, mcp_types.TextContent)
        ],
        result.structuredContent or {"ok": True},
    )


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await MCP_SERVER.run(
            read_stream,
            write_stream,
            MCP_SERVER.create_initialization_options(),
        )


def doctor_status(toolsets: tuple[str, ...]) -> dict[str, Any]:
    from . import zotero_setup

    result = zotero_setup.build_setup_report("full")
    result["toolsets"] = list(toolsets)
    return result


def codex_config_toml(toolsets: tuple[str, ...]) -> str:
    from . import zotero_setup

    return zotero_setup.codex_config_toml(toolsets)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--toolsets",
        type=parse_toolsets,
        default=DEFAULT_TOOLSETS,
        help=(
            f"Comma-separated toolsets: {', '.join((*TOOLSETS, 'all'))}. "
            "Default: literature."
        ),
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--doctor",
        action="store_true",
        help="Run read-only platform, dependency, Zotero Local API, and Web API checks.",
    )
    actions.add_argument(
        "--print-codex-config",
        action="store_true",
        help="Print a platform-specific Codex MCP config block and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    effective_argv = sys.argv[1:] if argv is None else argv
    if effective_argv and effective_argv[0] == "setup":
        from . import zotero_setup

        zotero_setup.main(effective_argv[1:])
        return
    arguments = _parse_args(effective_argv)
    configure_toolsets(arguments.toolsets)
    if arguments.doctor:
        print(
            json.dumps(doctor_status(arguments.toolsets), ensure_ascii=False, indent=2)
        )
        return
    if arguments.print_codex_config:
        print(codex_config_toml(arguments.toolsets))
        return
    anyio.run(run_server)


if __name__ == "__main__":
    main()
