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

SCHEMA_VERSION = 3
DEFAULT_DATABASE_NAME = "zotero_workflow.sqlite3"
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TRANSLATION_QUEUE_COLUMNS = {
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "downloaded_at": "TEXT NOT NULL DEFAULT ''",
    "next_attempt_at": "TEXT NOT NULL DEFAULT ''",
}

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


def ensure_translation_queue_columns(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='translation_queue'"
    ).fetchone()
    if table is None:
        return
    existing = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(translation_queue)")
    }
    for name, declaration in TRANSLATION_QUEUE_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE translation_queue ADD COLUMN {name} {declaration}"
            )


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
    ensure_translation_queue_columns(connection)
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version < SCHEMA_VERSION:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    if os.name != "nt":
        os.chmod(selected, 0o600)
    return connection


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
