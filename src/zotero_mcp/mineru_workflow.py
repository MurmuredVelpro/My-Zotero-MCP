#!/usr/bin/env python3
"""Recoverable Zotero collection workflow for MinerU batch parsing."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import mineru_client, workflow_database, zotero_local

OUTPUT_ROOT = mineru_client.DEFAULT_OUTPUT_ROOT
STAGING_DIR = OUTPUT_ROOT / ".staging"
WORKFLOW_DATABASE = workflow_database.default_database_path()
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_FILE_PAGES = 200


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def mineru_records_by_key(
    database: Path | None = None,
) -> dict[str, dict[str, str]]:
    return workflow_database.mineru_records_by_key(database or WORKFLOW_DATABASE)


def update_mineru_record(
    item_key: str,
    updates: dict[str, str],
    database: Path | None = None,
) -> bool:
    return workflow_database.update_mineru_record(
        item_key, updates, database or WORKFLOW_DATABASE
    )


def mark_mineru_stale(item_key: str, database: Path | None = None) -> bool:
    return update_mineru_record(
        item_key,
        {"has_pdf": "true", "mineru_parsed": "false", "qmd_indexed": "false"},
        database,
    )


def mark_mineru_parsed(
    item_key: str,
    attachment_key: str,
    database: Path | None = None,
) -> bool:
    return update_mineru_record(
        item_key,
        {
            "parsed_attachment_key": attachment_key,
            "has_pdf": "true",
            "mineru_parsed": "true",
            "qmd_indexed": "false",
        },
        database,
    )


def mineru_keys_needing_qmd(database: Path | None = None) -> list[str]:
    return workflow_database.mineru_keys_needing_qmd(database or WORKFLOW_DATABASE)


def mark_mineru_qmd_indexed(
    item_keys: list[str] | None = None,
    database: Path | None = None,
) -> list[str]:
    return workflow_database.mark_mineru_qmd_indexed(
        item_keys, database or WORKFLOW_DATABASE
    )


def tracked_result_status(
    item_key: str,
    current_attachment_key: str,
    database: Path | None = None,
) -> str:
    row = mineru_records_by_key(database).get(item_key)
    if row is None:
        return "untracked"
    if (
        row["mineru_parsed"] == "true"
        and row["parsed_attachment_key"] == current_attachment_key
    ):
        return "current"
    return "stale"


def sync_mineru_records_from_plan(
    plan: dict[str, Any], database: Path | None = None
) -> list[str]:
    return workflow_database.sync_mineru_records_from_plan(
        plan, database or WORKFLOW_DATABASE
    )


def save_state(state: dict[str, Any], database: Path | None = None) -> Path:
    return workflow_database.save_mineru_batch(state, database or WORKFLOW_DATABASE)


def load_state(batch_id: str, database: Path | None = None) -> dict[str, Any]:
    return workflow_database.load_mineru_batch(batch_id, database or WORKFLOW_DATABASE)


def list_states(database: Path | None = None) -> list[dict[str, Any]]:
    return workflow_database.list_mineru_batches(database or WORKFLOW_DATABASE)


def pdf_page_count(pdf: Path) -> int:
    result = subprocess.run(
        [zotero_local.pdf_tool_command("pdfinfo"), str(pdf)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    line = next(
        (line for line in result.stdout.splitlines() if line.startswith("Pages:")),
        None,
    )
    if line is None:
        raise RuntimeError(f"pdfinfo returned no page count: {pdf}")
    return int(line.split(":", 1)[1].strip())


def collection_plan(collection_key: str, recursive: bool = False) -> dict[str, Any]:
    collections = zotero_local.fetch_all_collections()
    index = zotero_local.collection_index(collections)
    items, _ = zotero_local.collect_collection_items(
        collection_key, index, recursive, 1000
    )
    plan: dict[str, Any] = {
        "collection_key": collection_key,
        "ready": [],
        "existing": [],
        "untracked_existing": [],
        "missing": [],
        "blocked": [],
    }
    mineru_records = mineru_records_by_key()
    for item in items:
        data = item.get("data", {})
        key = str(data.get("key") or item.get("key", "")).strip()
        title = str(data.get("title", "")).strip()
        existing = mineru_client.find_local_result(key)
        tracked = mineru_records.get(key)
        parsed_attachment_key = (
            str(tracked.get("parsed_attachment_key") or "") if tracked else ""
        )
        if existing and tracked and tracked["mineru_parsed"] == "true":
            attachments = zotero_local.pdf_attachments_for_item(item)
            if any(
                attachment["key"] == parsed_attachment_key for attachment in attachments
            ):
                plan["existing"].append(
                    {
                        "data_id": key,
                        "title": title,
                        "attachment_key": parsed_attachment_key,
                    }
                )
                continue
        try:
            attachment = zotero_local.english_pdf_attachment_for_item(item)
            pdf = Path(attachment["path"]).resolve()
        except SystemExit as exc:
            plan["missing"].append({"data_id": key, "title": title, "reason": str(exc)})
            continue
        pages = pdf_page_count(pdf)
        size_bytes = pdf.stat().st_size
        plan_status = (
            "untracked_existing"
            if existing and tracked is None
            else (
                "stale"
                if parsed_attachment_key
                and parsed_attachment_key != str(attachment["key"])
                else "ready"
            )
        )
        record = {
            "data_id": key,
            "title": title,
            "attachment_key": str(attachment["key"]),
            "parsed_attachment_key": parsed_attachment_key,
            "path": str(pdf),
            "name": pdf.name,
            "pages": pages,
            "size_bytes": size_bytes,
            "replace_existing": bool(existing),
            "plan_status": plan_status,
        }
        reasons = []
        if pages > MAX_FILE_PAGES:
            reasons.append(f"{pages} pages exceeds {MAX_FILE_PAGES}")
        if size_bytes > MAX_FILE_BYTES:
            reasons.append(f"{size_bytes} bytes exceeds {MAX_FILE_BYTES}")
        if plan_status == "untracked_existing":
            record["reason"] = (
                "complete local MinerU result exists without a SQLite state record; "
                "adopt explicitly before submitting"
            )
            plan["untracked_existing"].append(record)
            plan["blocked"].append(record)
        elif reasons:
            record["reason"] = "; ".join(reasons)
            plan["blocked"].append(record)
        else:
            plan["ready"].append(record)
    plan["item_count"] = len(items)
    plan["ready_pages"] = sum(record["pages"] for record in plan["ready"])
    return plan


def print_plan(plan: dict[str, Any]) -> None:
    for status in ("existing", "missing", "blocked", "ready"):
        for record in plan[status]:
            pages = f" pages={record['pages']}" if "pages" in record else ""
            reason = f" reason={record['reason']}" if record.get("reason") else ""
            label = record.get("plan_status", status).upper()
            attachment = (
                f" attachment={record['attachment_key']}"
                if record.get("attachment_key")
                else ""
            )
            parsed_attachment = (
                f" parsed_attachment={record['parsed_attachment_key']}"
                if record.get("parsed_attachment_key")
                else ""
            )
            print(
                f"{label} {record['data_id']}{pages}{attachment}"
                f"{parsed_attachment}{reason} "
                f"{record['title']}",
                flush=True,
            )
    print(
        "SUMMARY collection={} items={} ready={} stale={} pages={} existing={} "
        "untracked_existing={} missing={} blocked={}".format(
            plan["collection_key"],
            plan["item_count"],
            len(plan["ready"]),
            sum(record.get("plan_status") == "stale" for record in plan["ready"]),
            plan["ready_pages"],
            len(plan["existing"]),
            len(plan.get("untracked_existing", [])),
            len(plan["missing"]),
            len(plan["blocked"]),
        ),
        flush=True,
    )


def select_batch(
    ready: list[dict[str, Any]], max_files: int, max_pages: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not 1 <= max_files <= mineru_client.MAX_BATCH_FILES:
        raise ValueError(f"max_files must be 1-{mineru_client.MAX_BATCH_FILES}")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    selected = []
    deferred = []
    pages = 0
    for record in ready:
        if len(selected) >= max_files or pages + record["pages"] > max_pages:
            deferred.append(record)
            continue
        selected.append(record)
        pages += record["pages"]
    return selected, deferred


def upload_pending(state: dict[str, Any]) -> list[str]:
    failures = []
    for record in state["files"]:
        if record.get("uploaded"):
            continue
        try:
            status_code = mineru_client.upload_file(
                Path(record["path"]), record["upload_url"]
            )
            record.update(
                {
                    "uploaded": True,
                    "upload_status_code": status_code,
                    "uploaded_at": utc_now(),
                    "upload_error": None,
                }
            )
            print(f"UPLOADED {record['data_id']} HTTP={status_code}", flush=True)
        except Exception as exc:  # noqa: BLE001 - one upload must not hide later results
            record["upload_error"] = str(exc)
            failures.append(record["data_id"])
            print(f"UPLOAD_FAILED {record['data_id']} {exc}", flush=True)
        save_state(state)
    return failures


def verify_result_dir(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    missing = mineru_client.missing_result_artifacts(output_dir)
    if missing:
        raise RuntimeError(f"Missing or empty artifacts in {output_dir}: {missing}")
    for name in (
        "content_list.json",
        "content_list_v2.json",
        "model.json",
        "layout.json",
    ):
        json.loads((output_dir / name).read_text(encoding="utf-8"))
    with zipfile.ZipFile(output_dir / "result.zip") as archive:
        bad_member = archive.testzip()
    if bad_member:
        raise RuntimeError(f"Corrupt ZIP member in {output_dir}: {bad_member}")
    if not (output_dir / "origin.pdf").read_bytes()[:5].startswith(b"%PDF-"):
        raise RuntimeError(f"Invalid origin.pdf in {output_dir}")
    partials = [str(path) for path in output_dir.rglob("*.part")]
    if partials:
        raise RuntimeError(f"Partial files remain in {output_dir}: {partials}")

    markdown = (output_dir / "full.md").read_text(encoding="utf-8")
    broken_images = []
    for raw_target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
        target = raw_target.strip().strip("<>").split(maxsplit=1)[0]
        parsed = urlparse(target)
        if parsed.scheme or target.startswith("#"):
            continue
        image = output_dir / unquote(target)
        if not image.is_file():
            broken_images.append(target)
    if broken_images:
        raise RuntimeError(
            f"Broken Markdown image links in {output_dir}: {broken_images}"
        )
    return {
        "output_dir": str(output_dir),
        "artifact_count": len(mineru_client.REQUIRED_RESULT_ARTIFACTS),
        "broken_images": 0,
    }


def adopt_existing_plan(
    item_key: str,
    attachment_key: str,
    database: Path | None = None,
) -> dict[str, Any]:
    item_key = item_key.strip()
    attachment_key = attachment_key.strip()
    if not item_key or not attachment_key:
        raise ValueError("item_key and attachment_key are required")
    selected_database = (database or WORKFLOW_DATABASE).expanduser()
    if workflow_database.has_mineru_record(item_key, selected_database):
        raise RuntimeError(f"MinerU state already exists for item: {item_key}")

    output_dir = (OUTPUT_ROOT / item_key).expanduser().resolve()
    verification = verify_result_dir(output_dir)
    item = zotero_local.get_item(item_key)
    data = item.get("data") or {}
    actual_item_key = str(data.get("key") or item.get("key") or "")
    if actual_item_key != item_key:
        raise RuntimeError(
            f"Zotero item key mismatch: requested {item_key}, got {actual_item_key}"
        )
    attachments = zotero_local.pdf_attachments_for_item(item)
    attachment = next(
        (row for row in attachments if str(row.get("key")) == attachment_key), None
    )
    if attachment is None:
        raise RuntimeError(
            f"指定附件不是当前 Zotero 中可用的本地 PDF: {attachment_key}"
        )
    if zotero_local.has_translation_marker(
        str(attachment.get("title") or ""), str(attachment.get("filename") or "")
    ):
        raise RuntimeError(f"指定附件看起来是译文而非英文源 PDF: {attachment_key}")
    source_pdf = Path(str(attachment["path"])).expanduser().resolve()
    cjk_share, letters = zotero_local.pdf_text_language_stats(source_pdf)
    if letters < 200 or cjk_share >= zotero_local.ENGLISH_PDF_MAX_CJK_SHARE:
        raise RuntimeError(
            f"指定附件未通过英文正文检查: {attachment_key} "
            f"letters={letters} cjk_share={cjk_share:.3f}"
        )
    source_pages = pdf_page_count(source_pdf)
    origin_pages = pdf_page_count(output_dir / "origin.pdf")
    if source_pages != origin_pages:
        raise RuntimeError(
            f"source/origin page count mismatch: source={source_pages} "
            f"origin={origin_pages}"
        )
    return {
        "item_key": item_key,
        "attachment_key": attachment_key,
        "source_pdf": str(source_pdf),
        "output_dir": str(output_dir),
        "source_pages": source_pages,
        "origin_pages": origin_pages,
        "verification": verification,
        "status": "ready",
    }


def adopt_existing(
    plan: dict[str, Any], database: Path | None = None
) -> dict[str, Any]:
    if plan.get("status") != "ready":
        raise RuntimeError("adopt-existing plan is not ready")
    workflow_database.adopt_mineru_record(
        str(plan["item_key"]), str(plan["attachment_key"]), database
    )
    return {**plan, "status": "adopted", "qmd_indexed": False}


def cmd_plan(arguments: argparse.Namespace) -> int:
    print_plan(collection_plan(arguments.collection_key, arguments.recursive))
    return 0


def cmd_adopt_existing(arguments: argparse.Namespace) -> int:
    database = Path(getattr(arguments, "database", WORKFLOW_DATABASE))
    plan = adopt_existing_plan(
        arguments.item_key,
        arguments.attachment_key,
        database,
    )
    if arguments.confirm:
        plan = adopt_existing(plan, database)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def submit_selected_batch(
    collection_key: str,
    selected: list[dict[str, Any]],
    *,
    deferred_count: int,
    max_pages: int,
) -> tuple[dict[str, Any], list[str]]:
    if not selected:
        raise RuntimeError("No eligible PDF fits the requested batch limits")
    untracked = [
        record["data_id"]
        for record in selected
        if record.get("plan_status") in {"untracked", "untracked_existing"}
    ]
    if untracked:
        raise RuntimeError(
            "Cannot submit untracked existing MinerU results; adopt first: "
            + ", ".join(untracked)
        )
    for record in selected:
        if record.get("plan_status") == "stale":
            mark_mineru_stale(record["data_id"])
    specs = [
        {"name": record["name"], "data_id": record["data_id"], "is_ocr": False}
        for record in selected
    ]
    batch = mineru_client.request_upload_batch(
        specs,
        model_version="vlm",
        language="en",
        enable_formula=True,
        enable_table=True,
    )
    state = {
        "batch_id": batch["batch_id"],
        "collection_key": collection_key,
        "created_at": utc_now(),
        "status": "uploading",
        "model_version": "vlm",
        "language": "en",
        "max_pages": max_pages,
        "selected_pages": sum(record["pages"] for record in selected),
        "deferred_count": deferred_count,
        "files": [],
    }
    for record, upload_url in zip(selected, batch["file_urls"], strict=True):
        state["files"].append(
            {
                **record,
                "upload_url": upload_url,
                "uploaded": False,
                "downloaded": False,
                "result_state": "waiting-file",
            }
        )
    path = save_state(state)
    print(
        "BATCH_CREATED batch_id={} files={} pages={} state={}".format(
            state["batch_id"],
            len(state["files"]),
            state["selected_pages"],
            path,
        ),
        flush=True,
    )
    failures = upload_pending(state)
    state["status"] = "partial-upload" if failures else "uploaded"
    save_state(state)
    print(
        f"UPLOAD_SUMMARY batch_id={state['batch_id']} "
        f"uploaded={len(state['files']) - len(failures)} failed={len(failures)}",
        flush=True,
    )
    return state, failures


def cmd_submit_batch(arguments: argparse.Namespace) -> int:
    plan = collection_plan(arguments.collection_key, arguments.recursive)
    sync_mineru_records_from_plan(plan)
    print_plan(plan)
    selected, deferred = select_batch(
        plan["ready"], arguments.max_files, arguments.max_pages
    )
    _, failures = submit_selected_batch(
        arguments.collection_key,
        selected,
        deferred_count=len(deferred),
        max_pages=arguments.max_pages,
    )
    return 1 if failures else 0


def match_result(
    result: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any] | None:
    data_id = str(result.get("data_id") or "")
    if data_id:
        return next(
            (record for record in records if record["data_id"] == data_id), None
        )
    file_name = str(result.get("file_name") or "")
    return next((record for record in records if record["name"] == file_name), None)


def collect_batch(batch_id: str) -> tuple[dict[str, Any], int]:
    state = load_state(batch_id)
    upload_failures = upload_pending(state)
    batch = mineru_client.get_batch(state["batch_id"])
    results = mineru_client.extract_results(batch)
    for result in results:
        record = match_result(result, state["files"])
        if record is None:
            print(
                f"UNMATCHED_RESULT {result.get('data_id')} {result.get('file_name')}",
                flush=True,
            )
            continue
        result_state = str(result.get("state") or "unknown")
        record["result_state"] = result_state
        record["result_error"] = result.get("err_msg")
        if result.get("extract_progress"):
            record["extract_progress"] = result["extract_progress"]
        if result_state == "done" and not record.get("downloaded"):
            full_zip_url = str(result.get("full_zip_url") or "")
            if not full_zip_url:
                raise RuntimeError(
                    f"MinerU task done without full_zip_url: {record['data_id']}"
                )
            output_dir = OUTPUT_ROOT / record["data_id"]
            if record.get("replace_existing") and output_dir.is_dir():
                batch_dir = STAGING_DIR / state["batch_id"]
                staged_dir = batch_dir / record["data_id"]
                mineru_client.download_and_extract(full_zip_url, staged_dir)
                verify_result_dir(staged_dir)
                recovery_dir = batch_dir / f"{record['data_id']}.previous"
                mineru_client.install_result_directory(
                    staged_dir, output_dir, recovery_dir
                )
            else:
                mineru_client.download_and_extract(full_zip_url, output_dir)
            record["verification"] = verify_result_dir(output_dir)
            record["downloaded"] = True
            record["downloaded_at"] = utc_now()
            save_state(state)
        if result_state == "done" and record.get("downloaded"):
            attachment_key = str(record.get("attachment_key") or "")
            if attachment_key:
                mark_mineru_parsed(record["data_id"], attachment_key)
        print(
            f"RESULT {record['data_id']} state={result_state} "
            f"downloaded={str(bool(record.get('downloaded'))).lower()}",
            flush=True,
        )

    terminal = {"done", "failed"}
    all_terminal = all(
        record.get("result_state") in terminal for record in state["files"]
    )
    all_done_downloaded = all(
        record.get("result_state") == "failed" or record.get("downloaded")
        for record in state["files"]
    )
    state["status"] = (
        "complete" if all_terminal and all_done_downloaded else "processing"
    )
    save_state(state)
    counts: dict[str, int] = {}
    for record in state["files"]:
        result_state = str(record.get("result_state") or "unknown")
        counts[result_state] = counts.get(result_state, 0) + 1
    print(
        f"COLLECT_SUMMARY batch_id={state['batch_id']} status={state['status']} "
        f"states={json.dumps(counts, sort_keys=True)}",
        flush=True,
    )
    failed = counts.get("failed", 0) + len(upload_failures)
    return state, failed


def cmd_collect(arguments: argparse.Namespace) -> int:
    _, failed = collect_batch(arguments.batch_id)
    return 1 if failed else 0


def verify_target(target: str) -> dict[str, Any]:
    item_dir = OUTPUT_ROOT / target
    if item_dir.is_dir():
        return {target: verify_result_dir(item_dir)}
    state = load_state(target)
    result = {}
    for record in state["files"]:
        result[record["data_id"]] = verify_result_dir(OUTPUT_ROOT / record["data_id"])
    return result


def cmd_verify(arguments: argparse.Namespace) -> int:
    result = verify_target(arguments.target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="Read-only collection preflight")
    plan.add_argument("collection_key")
    plan.add_argument("--recursive", action="store_true")
    plan.set_defaults(handler=cmd_plan)

    adopt = commands.add_parser(
        "adopt-existing", help="Adopt one existing MinerU result into SQLite"
    )
    adopt.add_argument("item_key")
    adopt.add_argument("--attachment-key", required=True)
    adopt.add_argument("--database", type=Path, default=WORKFLOW_DATABASE)
    adopt.add_argument("--confirm", action="store_true")
    adopt.set_defaults(handler=cmd_adopt_existing)

    submit = commands.add_parser(
        "submit-batch", help="Create and upload one recoverable MinerU batch"
    )
    submit.add_argument("collection_key")
    submit.add_argument("--max-pages", type=int, required=True)
    submit.add_argument("--max-files", type=int, default=50)
    submit.add_argument("--recursive", action="store_true")
    submit.set_defaults(handler=cmd_submit_batch)

    collect = commands.add_parser(
        "collect", help="Resume uploads, query once, and download completed results"
    )
    collect.add_argument("batch_id", help="MinerU batch_id or latest")
    collect.set_defaults(handler=cmd_collect)

    verify = commands.add_parser("verify", help="Verify one item or one batch")
    verify.add_argument("target", help="Zotero item key, batch_id, or latest")
    verify.set_defaults(handler=cmd_verify)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return int(arguments.handler(arguments))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports unexpected failures
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
