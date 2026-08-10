"""Unified full-text extraction with MinerU-first source selection."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import mineru_client, zotero_local

_MINERU_WORKFLOW: ModuleType | None = None


ATTACHMENT_PRIORITIES = {
    "mineru_then_local": ("mineru", "pdftotext"),
    "mineru_only": ("mineru",),
    "local_only": ("pdftotext",),
}


class ExtractionError(RuntimeError):
    """Raised when no configured full-text source can produce usable text."""


class MinerUWorkflowUnavailable(ExtractionError):
    """Raised when the optional local MinerU workflow is unavailable."""


def load_mineru_workflow() -> ModuleType:
    global _MINERU_WORKFLOW

    if _MINERU_WORKFLOW is not None:
        return _MINERU_WORKFLOW
    try:
        module = importlib.import_module("mineru_workflow")
    except ModuleNotFoundError as exc:
        if exc.name != "mineru_workflow":
            raise
        raise MinerUWorkflowUnavailable(
            "Optional MinerU workflow module is not installed. Reinstall zotero-mcp."
        ) from exc
    _MINERU_WORKFLOW = module
    return module


@dataclass(frozen=True)
class ExtractedDoc:
    text: str
    item_key: str
    attachment_key: str
    source: str
    source_path: Path
    output_path: Path

    def __bool__(self) -> bool:
        return bool(self.text.strip())


def extract_document(
    item: dict[str, Any],
    *,
    attachment_priority: str = "mineru_then_local",
    out_dir: str | None = None,
    output: str | None = None,
) -> ExtractedDoc:
    priority = ATTACHMENT_PRIORITIES.get(attachment_priority)
    if priority is None:
        expected = ", ".join(ATTACHMENT_PRIORITIES)
        raise ExtractionError(
            f"Unknown attachment_priority: {attachment_priority}. Expected one of: {expected}."
        )

    data = item.get("data", {})
    item_key = str(data.get("key") or item.get("key") or "")
    if not item_key:
        raise ExtractionError("Zotero item has no item key.")

    attachment = zotero_local.english_pdf_attachment_for_item(item)
    attachment_key = str(attachment["key"])
    output_path = zotero_local.resolve_text_output_path(output, out_dir, item)
    failures: list[str] = []

    for source in priority:
        try:
            if source == "mineru":
                return _extract_mineru(
                    item_key=item_key,
                    attachment_key=attachment_key,
                    output_path=output_path,
                )
            return _extract_pdftotext(
                item_key=item_key,
                attachment_key=attachment_key,
                pdf_path=Path(attachment["path"]),
                output_path=output_path,
            )
        except ExtractionError as exc:
            failures.append(f"{source}: {exc}")

    raise ExtractionError("No usable full-text source. " + "; ".join(failures))


def _extract_mineru(
    *, item_key: str, attachment_key: str, output_path: Path
) -> ExtractedDoc:
    result = mineru_client.find_local_result(item_key)
    if not result:
        raise ExtractionError("no local MinerU result")

    status = load_mineru_workflow().tracked_result_status(item_key, attachment_key)
    if status != "current":
        raise ExtractionError(f"result status is {status}")

    source_path = Path(str(result.get("full_md") or ""))
    if not source_path.is_file():
        raise ExtractionError("full.md is missing")
    text = source_path.read_text(encoding="utf-8")
    if not text.strip():
        raise ExtractionError("full.md is empty")

    if source_path.resolve() != output_path.resolve():
        output_path.write_text(text, encoding="utf-8")
    return ExtractedDoc(
        text=text,
        item_key=item_key,
        attachment_key=attachment_key,
        source="mineru",
        source_path=source_path,
        output_path=output_path,
    )


def _extract_pdftotext(
    *, item_key: str, attachment_key: str, pdf_path: Path, output_path: Path
) -> ExtractedDoc:
    try:
        zotero_local.run_command(
            [
                zotero_local.pdf_tool_command("pdftotext"),
                "-layout",
                "-enc",
                "UTF-8",
                str(pdf_path),
                str(output_path),
            ]
        )
    except (OSError, SystemExit) as exc:
        raise ExtractionError(str(exc)) from exc

    if not output_path.is_file():
        raise ExtractionError("pdftotext did not create the output file")
    text = output_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ExtractionError("pdftotext produced empty text")
    return ExtractedDoc(
        text=text,
        item_key=item_key,
        attachment_key=attachment_key,
        source="pdftotext",
        source_path=pdf_path,
        output_path=output_path,
    )
