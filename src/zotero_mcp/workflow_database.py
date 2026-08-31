#!/usr/bin/env python3
"""Persistent SQLite state shared by Zotero workflow commands."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import zotero_translate

DEFAULT_DATABASE_NAME = "zotero_workflow.sqlite3"
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

MINERU_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mineru_item_state (
    item_key TEXT PRIMARY KEY,
    parsed_attachment_key TEXT NOT NULL,
    has_pdf INTEGER NOT NULL CHECK (has_pdf IN (0, 1)),
    mineru_parsed INTEGER NOT NULL CHECK (mineru_parsed IN (0, 1)),
    qmd_indexed INTEGER NOT NULL CHECK (qmd_indexed IN (0, 1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mineru_batches (
    batch_id TEXT PRIMARY KEY,
    collection_key TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mineru_batch_files (
    batch_id TEXT NOT NULL REFERENCES mineru_batches(batch_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    source_attachment_key TEXT NOT NULL,
    result_state TEXT NOT NULL,
    uploaded INTEGER NOT NULL CHECK (uploaded IN (0, 1)),
    downloaded INTEGER NOT NULL CHECK (downloaded IN (0, 1)),
    PRIMARY KEY (batch_id, position)
);
CREATE INDEX IF NOT EXISTS mineru_batch_files_item_idx
    ON mineru_batch_files(item_key);
CREATE INDEX IF NOT EXISTS mineru_batches_collection_idx
    ON mineru_batches(collection_key, updated_at);
"""

PDF_ACQUISITION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pdf_acquisition (
    item_key TEXT PRIMARY KEY REFERENCES items(item_key),
    state TEXT NOT NULL,
    candidate_url TEXT,
    source_kind TEXT,
    version_kind TEXT,
    access_kind TEXT,
    evidence_json TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    next_check_at TEXT,
    downloaded_at TEXT,
    last_error TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pdf_acquisition_state_idx
    ON pdf_acquisition(state, next_check_at);
"""

PDF_ACQUISITION_STATES = {
    "unchecked",
    "not_needed",
    "eligible_publisher_vor",
    "eligible_pmc_vor",
    "eligible_official_preprint",
    "manual_version_unproven",
    "manual_no_vor_found",
    "blocked",
    "failed_validation",
    "downloaded",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_database_path() -> Path:
    return zotero_translate.state_dir() / DEFAULT_DATABASE_NAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    selected = (path or default_database_path()).expanduser()
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(selected)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(MINERU_SCHEMA_SQL)
    connection.executescript(PDF_ACQUISITION_SCHEMA_SQL)
    if os.name != "nt":
        os.chmod(selected, 0o600)
    return connection


def save_pdf_acquisition_records(
    records: list[dict[str, Any]], path: Path | None = None
) -> None:
    with connect(path) as connection:
        for record in records:
            item_key = str(record.get("item_key") or "").strip().upper()
            state = str(record.get("state") or "").strip()
            if not item_key:
                raise ValueError("PDF acquisition item_key is required")
            if state not in PDF_ACQUISITION_STATES:
                raise ValueError(f"Invalid PDF acquisition state: {state}")
            existing = connection.execute(
                "SELECT state FROM pdf_acquisition WHERE item_key=?", (item_key,)
            ).fetchone()
            if existing is not None and (
                (
                    state == "blocked"
                    and (
                        str(existing["state"]).startswith("eligible_")
                        or existing["state"] == "downloaded"
                    )
                )
                or (state == "not_needed" and existing["state"] == "downloaded")
            ):
                connection.execute(
                    """
                    UPDATE pdf_acquisition
                    SET checked_at=?, next_check_at=?, last_error=?
                    WHERE item_key=?
                    """,
                    (
                        str(record.get("checked_at") or ""),
                        record.get("next_check_at"),
                        str(record.get("last_error") or ""),
                        item_key,
                    ),
                )
                continue
            values = {
                "item_key": item_key,
                "state": state,
                "candidate_url": record.get("candidate_url"),
                "source_kind": record.get("source_kind"),
                "version_kind": record.get("version_kind"),
                "access_kind": record.get("access_kind"),
                "evidence_json": str(record.get("evidence_json") or "{}"),
                "checked_at": str(record.get("checked_at") or ""),
                "next_check_at": record.get("next_check_at"),
                "downloaded_at": record.get("downloaded_at"),
                "last_error": str(record.get("last_error") or ""),
            }
            try:
                connection.execute(
                    """
                    INSERT INTO pdf_acquisition(
                        item_key, state, candidate_url, source_kind, version_kind,
                        access_kind, evidence_json, checked_at, next_check_at,
                        downloaded_at, last_error
                    ) VALUES(
                        :item_key, :state, :candidate_url, :source_kind,
                        :version_kind, :access_kind, :evidence_json, :checked_at,
                        :next_check_at, :downloaded_at, :last_error
                    )
                    ON CONFLICT(item_key) DO UPDATE SET
                        state=excluded.state,
                        candidate_url=excluded.candidate_url,
                        source_kind=excluded.source_kind,
                        version_kind=excluded.version_kind,
                        access_kind=excluded.access_kind,
                        evidence_json=excluded.evidence_json,
                        checked_at=excluded.checked_at,
                        next_check_at=excluded.next_check_at,
                        downloaded_at=COALESCE(
                            excluded.downloaded_at, pdf_acquisition.downloaded_at
                        ),
                        last_error=excluded.last_error
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"workflow item is missing for PDF acquisition: {item_key}; "
                    "run zotero-workflow sync first"
                ) from exc


def pdf_acquisition_record(
    item_key: str, path: Path | None = None
) -> dict[str, Any] | None:
    selected = (path or default_database_path()).expanduser()
    if not selected.is_file():
        return None
    with connect(selected) as connection:
        row = connection.execute(
            "SELECT * FROM pdf_acquisition WHERE item_key=?",
            (item_key.strip().upper(),),
        ).fetchone()
    return dict(row) if row else None


def mark_pdf_acquisition_failed(
    item_key: str, error: str, path: Path | None = None
) -> None:
    with connect(path) as connection:
        cursor = connection.execute(
            """
            UPDATE pdf_acquisition
            SET state='failed_validation', checked_at=?, last_error=?
            WHERE item_key=?
            """,
            (utc_now(), error, item_key.strip().upper()),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"No PDF acquisition state for item: {item_key}")


def mark_pdf_acquisition_downloaded(
    item_key: str, downloaded_at: str, path: Path | None = None
) -> None:
    with connect(path) as connection:
        cursor = connection.execute(
            """
            UPDATE pdf_acquisition
            SET state='downloaded', checked_at=?, downloaded_at=?,
                next_check_at=NULL, last_error=''
            WHERE item_key=?
            """,
            (downloaded_at, downloaded_at, item_key.strip().upper()),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"No PDF acquisition state for item: {item_key}")


def mineru_records_by_key(path: Path | None = None) -> dict[str, dict[str, str]]:
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT item_key, parsed_attachment_key, has_pdf,
                   mineru_parsed, qmd_indexed
            FROM mineru_item_state
            ORDER BY item_key
            """
        ).fetchall()
    return {
        str(row["item_key"]): {
            "item_key": str(row["item_key"]),
            "parsed_attachment_key": str(row["parsed_attachment_key"]),
            "has_pdf": str(bool(row["has_pdf"])).lower(),
            "mineru_parsed": str(bool(row["mineru_parsed"])).lower(),
            "qmd_indexed": str(bool(row["qmd_indexed"])).lower(),
        }
        for row in rows
    }


def has_mineru_record(item_key: str, path: Path | None = None) -> bool:
    selected = (path or default_database_path()).expanduser()
    if not selected.is_file():
        return False
    uri = f"file:{selected}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM mineru_item_state WHERE item_key=?", (item_key,)
                ).fetchone()
                is not None
            )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise


def update_mineru_record(
    item_key: str,
    updates: dict[str, str],
    path: Path | None = None,
) -> bool:
    allowed = {
        "parsed_attachment_key",
        "has_pdf",
        "mineru_parsed",
        "qmd_indexed",
    }
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"Unknown MinerU state fields: {sorted(unknown)}")
    values: dict[str, Any] = {"item_key": item_key, "updated_at": utc_now()}
    assignments = []
    for field, value in updates.items():
        assignments.append(f"{field}=:{field}")
        values[field] = (
            int(value == "true")
            if field in {"has_pdf", "mineru_parsed", "qmd_indexed"}
            else value
        )
    if not assignments:
        return False
    with connect(path) as connection:
        existing = connection.execute(
            "SELECT * FROM mineru_item_state WHERE item_key=?", (item_key,)
        ).fetchone()
        if existing is None:
            return False
        changed = any(existing[field] != values[field] for field in updates)
        if not changed:
            return False
        connection.execute(
            f"UPDATE mineru_item_state SET {', '.join(assignments)}, "
            "updated_at=:updated_at WHERE item_key=:item_key",
            values,
        )
    return True


def adopt_mineru_record(
    item_key: str,
    attachment_key: str,
    path: Path | None = None,
) -> None:
    item_key = item_key.strip()
    attachment_key = attachment_key.strip()
    if not item_key or not attachment_key:
        raise ValueError("item_key and attachment_key are required")
    with connect(path) as connection:
        existing = connection.execute(
            "SELECT 1 FROM mineru_item_state WHERE item_key=?", (item_key,)
        ).fetchone()
        if existing is not None:
            raise ValueError(f"MinerU state already exists for item: {item_key}")
        connection.execute(
            """
            INSERT INTO mineru_item_state(
                item_key, parsed_attachment_key, has_pdf,
                mineru_parsed, qmd_indexed, updated_at
            ) VALUES(?, ?, 1, 1, 0, ?)
            """,
            (item_key, attachment_key, utc_now()),
        )


def sync_mineru_records_from_plan(
    plan: dict[str, Any], path: Path | None = None
) -> list[str]:
    observed_at = utc_now()
    added: list[str] = []
    with connect(path) as connection:
        for status in ("existing", "ready", "blocked", "missing"):
            has_pdf = int(status != "missing")
            for record in plan[status]:
                if record.get("plan_status") in {"untracked", "untracked_existing"}:
                    continue
                item_key = str(record["data_id"])
                existing = connection.execute(
                    "SELECT has_pdf FROM mineru_item_state WHERE item_key=?",
                    (item_key,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO mineru_item_state(
                            item_key, parsed_attachment_key, has_pdf,
                            mineru_parsed, qmd_indexed, updated_at
                        ) VALUES(?, '', ?, 0, 0, ?)
                        """,
                        (item_key, has_pdf, observed_at),
                    )
                    added.append(item_key)
                elif existing["has_pdf"] != has_pdf:
                    if has_pdf:
                        connection.execute(
                            "UPDATE mineru_item_state SET has_pdf=1, updated_at=? "
                            "WHERE item_key=?",
                            (observed_at, item_key),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE mineru_item_state
                            SET has_pdf=0, mineru_parsed=0, qmd_indexed=0,
                                updated_at=?
                            WHERE item_key=?
                            """,
                            (observed_at, item_key),
                        )
    return added


def mineru_keys_needing_qmd(path: Path | None = None) -> list[str]:
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT item_key FROM mineru_item_state
            WHERE mineru_parsed=1 AND qmd_indexed=0
            ORDER BY item_key
            """
        ).fetchall()
    return [str(row["item_key"]) for row in rows]


def mark_mineru_qmd_indexed(
    item_keys: list[str] | None = None, path: Path | None = None
) -> list[str]:
    with connect(path) as connection:
        if item_keys is None:
            rows = connection.execute(
                """
                SELECT item_key FROM mineru_item_state
                WHERE mineru_parsed=1 AND qmd_indexed=0
                ORDER BY item_key
                """
            ).fetchall()
        elif not item_keys:
            return []
        else:
            placeholders = ",".join("?" for _ in item_keys)
            rows = connection.execute(
                f"""
                SELECT item_key FROM mineru_item_state
                WHERE mineru_parsed=1 AND qmd_indexed=0
                  AND item_key IN ({placeholders})
                ORDER BY item_key
                """,
                item_keys,
            ).fetchall()
        changed = [str(row["item_key"]) for row in rows]
        if changed:
            placeholders = ",".join("?" for _ in changed)
            connection.execute(
                f"UPDATE mineru_item_state SET qmd_indexed=1, updated_at=? "
                f"WHERE item_key IN ({placeholders})",
                [utc_now(), *changed],
            )
    return changed


def _validate_batch_state(state: dict[str, Any]) -> str:
    batch_id = str(state.get("batch_id") or "")
    if not BATCH_ID_RE.fullmatch(batch_id):
        raise ValueError(f"Invalid batch_id: {batch_id}")
    files = state.get("files")
    if not isinstance(files, list):
        raise TypeError(f"MinerU batch files must be a list: {batch_id}")
    return batch_id


def _save_mineru_batch(connection: sqlite3.Connection, state: dict[str, Any]) -> None:
    batch_id = _validate_batch_state(state)
    state_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        """
        INSERT INTO mineru_batches(
            batch_id, collection_key, status, created_at, updated_at, state_json
        ) VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id) DO UPDATE SET
            collection_key=excluded.collection_key,
            status=excluded.status,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at,
            state_json=excluded.state_json
        """,
        (
            batch_id,
            str(state.get("collection_key") or ""),
            str(state.get("status") or ""),
            str(state.get("created_at") or ""),
            str(state.get("updated_at") or ""),
            state_json,
        ),
    )
    connection.execute("DELETE FROM mineru_batch_files WHERE batch_id=?", (batch_id,))
    connection.executemany(
        """
        INSERT INTO mineru_batch_files(
            batch_id, position, item_key, source_attachment_key,
            result_state, uploaded, downloaded
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                batch_id,
                position,
                str(record.get("data_id") or ""),
                str(record.get("attachment_key") or ""),
                str(record.get("result_state") or ""),
                int(bool(record.get("uploaded"))),
                int(bool(record.get("downloaded"))),
            )
            for position, record in enumerate(state["files"])
            if isinstance(record, dict)
        ],
    )


def save_mineru_batch(state: dict[str, Any], path: Path | None = None) -> Path:
    selected = (path or default_database_path()).expanduser()
    state["updated_at"] = utc_now()
    with connect(selected) as connection:
        _save_mineru_batch(connection, state)
    return selected


def load_mineru_batch(batch_id: str, path: Path | None = None) -> dict[str, Any]:
    with connect(path) as connection:
        if batch_id == "latest":
            row = connection.execute(
                """
                SELECT state_json FROM mineru_batches
                ORDER BY updated_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
        else:
            if not BATCH_ID_RE.fullmatch(batch_id):
                raise ValueError(f"Invalid batch_id: {batch_id}")
            row = connection.execute(
                "SELECT state_json FROM mineru_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
    if row is None:
        raise FileNotFoundError(f"No MinerU batch state found: {batch_id}")
    state = json.loads(row["state_json"])
    loaded_batch_id = str(state.get("batch_id") or "")
    if not BATCH_ID_RE.fullmatch(loaded_batch_id):
        raise ValueError("MinerU batch state has an invalid batch_id")
    if batch_id != "latest" and loaded_batch_id != batch_id:
        raise ValueError("MinerU batch state has an inconsistent batch_id")
    return state


def list_mineru_batches(path: Path | None = None) -> list[dict[str, Any]]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT state_json FROM mineru_batches ORDER BY updated_at, rowid"
        ).fetchall()
    return [json.loads(row["state_json"]) for row in rows]
