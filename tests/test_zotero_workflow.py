import copy
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from zotero_mcp import workflow_database, zotero_workflow


class WorkflowStoreTests(unittest.TestCase):
    def test_translation_queue_states_preserve_worker_semantics(self):
        expected = {
            "pending": "queued",
            "waiting": "waiting",
            "retry_wait": "retry_scheduled",
            "translating": "translating",
            "importing": "importing",
            "failed": "failed",
        }
        for queue_state, workflow_state in expected.items():
            with self.subTest(queue_state=queue_state):
                state, issues = zotero_workflow._translation_state(
                    0, False, False, queue_state, "PDF1"
                )
                self.assertEqual(state, workflow_state)
                self.assertEqual(issues, [])

        state, issues = zotero_workflow._translation_state(
            0, False, False, "done", "PDF1"
        )
        self.assertEqual(state, "done_missing_attachment")
        self.assertEqual(issues, ["translation_queue_done_but_attachment_missing"])

    def test_live_translation_attachment_beats_queue_state(self):
        self.assertEqual(
            zotero_workflow._translation_state(1, True, False, "failed", "PDF1"),
            ("translated_standard", []),
        )

    def test_pipeline_gate_requires_current_mineru_and_qmd_flags(self):
        record = {
            "parsed_attachment_key": "EN12PDF3",
            "mineru_parsed": "true",
            "qmd_indexed": "false",
        }
        self.assertEqual(
            zotero_workflow._pipeline_gate_states(record, "EN12PDF3", True, True, True)[
                :2
            ],
            ("parsed_current", "pending_index"),
        )
        record["qmd_indexed"] = "true"
        self.assertEqual(
            zotero_workflow._pipeline_gate_states(
                record, "EN12PDF3", True, True, False
            )[:2],
            ("parsed_current", "missing"),
        )
        self.assertEqual(
            zotero_workflow._pipeline_gate_states(record, "EN12PDF3", True, True, True)[
                :2
            ],
            ("parsed_current", "indexed_current"),
        )

    def test_pipeline_gate_rejects_old_source_and_unconfirmed_parse(self):
        stale, qmd, issues = zotero_workflow._pipeline_gate_states(
            {
                "parsed_attachment_key": "OLDPDF1",
                "mineru_parsed": "true",
                "qmd_indexed": "true",
            },
            "NEWPDF1",
            True,
            True,
            True,
        )
        self.assertEqual((stale, qmd), ("stale_source", "stale_source"))
        self.assertIn("mineru_stale_source", issues)

        pending, qmd, issues = zotero_workflow._pipeline_gate_states(
            {
                "parsed_attachment_key": "NEWPDF1",
                "mineru_parsed": "false",
                "qmd_indexed": "false",
            },
            "NEWPDF1",
            True,
            True,
            True,
        )
        self.assertEqual((pending, qmd), ("pending_parse", "stale_source"))
        self.assertIn("mineru_parse_unconfirmed", issues)

    def snapshot(self, observed_at="2026-08-20T12:00:00+00:00"):
        item = {
            "item_key": "AB12CD34",
            "item_type": "journalArticle",
            "title": "Example paper",
            "authors": "Example 等",
            "date": "2026",
            "date_added": "2026-08-20T11:00:00Z",
            "date_modified": "2026-08-20T11:30:00Z",
            "doi": "10.1000/example",
            "publication_title": "Example Journal",
            "zotero_version": 12,
            "source_attachment_key": "EN12PDF3",
            "source_error": "",
            "translation_state": "translated_standard",
            "mineru_state": "parsed_current",
            "qmd_state": "indexed_current",
            "issue": "",
            "last_seen_at": observed_at,
        }
        attachments = [
            {
                "attachment_key": "EN12PDF3",
                "item_key": "AB12CD34",
                "title": "PDF",
                "filename": "paper.pdf",
                "content_type": "application/pdf",
                "link_mode": "imported_file",
                "role": "source_pdf",
                "language": "en",
                "is_primary": 1,
                "is_standard_title": 0,
                "translation_of_attachment_key": None,
                "local_path": "/data/EN12PDF3/paper.pdf",
                "last_seen_at": observed_at,
            },
            {
                "attachment_key": "CN34PDF5",
                "item_key": "AB12CD34",
                "title": "CN",
                "filename": "paper-cn.pdf",
                "content_type": "application/pdf",
                "link_mode": "imported_file",
                "role": "translated_pdf",
                "language": "zh",
                "is_primary": 0,
                "is_standard_title": 1,
                "translation_of_attachment_key": "EN12PDF3",
                "local_path": "/data/CN34PDF5/paper-cn.pdf",
                "last_seen_at": observed_at,
            },
            {
                "attachment_key": "SI56PDF7",
                "item_key": "AB12CD34",
                "title": "SI",
                "filename": "paper-si.pdf",
                "content_type": "application/pdf",
                "link_mode": "imported_file",
                "role": "supplementary_pdf",
                "language": "unknown",
                "is_primary": 0,
                "is_standard_title": 0,
                "translation_of_attachment_key": None,
                "local_path": "/data/SI56PDF7/paper-si.pdf",
                "last_seen_at": observed_at,
            },
        ]
        return zotero_workflow.WorkflowSnapshot(
            observed_at=observed_at,
            roots=[
                {
                    "key": "XM2WM4D4",
                    "name": "Senescence",
                    "path": "Senescence",
                    "parent_key": None,
                    "match_kind": "name",
                }
            ],
            items=[item],
            memberships=[
                {
                    "item_key": "AB12CD34",
                    "collection_key": "XM2WM4D4",
                    "collection_path": "Senescence",
                }
            ],
            attachments=attachments,
            mineru_documents=[
                {
                    "item_key": "AB12CD34",
                    "parsed_attachment_key": "EN12PDF3",
                    "full_md_path": "/data/AB12CD34/full.md",
                    "state": "parsed_current",
                    "checked_at": observed_at,
                }
            ],
            qmd_documents=[
                {
                    "item_key": "AB12CD34",
                    "document_ref": "qmd://zotero-mineru/AB12CD34/full.md",
                    "docid": "#doc1",
                    "state": "indexed_current",
                    "checked_at": observed_at,
                }
            ],
            translation_queue=[],
            pdf2zh_tasks=[],
            system_health=[
                {
                    "system_name": "zotero",
                    "status": "ok",
                    "detail": {"scope_items": 1},
                    "checked_at": observed_at,
                    "error": "",
                }
            ],
        )

    def test_store_records_three_pdf_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            zotero_workflow.store_snapshot(database, self.snapshot())
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT attachment_key, role, translation_of_attachment_key "
                    "FROM pdf_attachments ORDER BY attachment_key"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("CN34PDF5", "translated_pdf", "EN12PDF3"),
                    ("EN12PDF3", "source_pdf", None),
                    ("SI56PDF7", "supplementary_pdf", None),
                ],
            )

    def test_status_returns_item_and_pipeline_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            zotero_workflow.store_snapshot(database, self.snapshot())
            data = zotero_workflow.status_data(database, "AB12CD34")
            self.assertEqual(data["item"]["translation_state"], "translated_standard")
            self.assertEqual(data["mineru"]["parsed_attachment_key"], "EN12PDF3")
            self.assertEqual(data["qmd"]["state"], "indexed_current")
            self.assertEqual(len(data["attachments"]), 3)

    def test_translation_queue_schema_migrates_and_stores_retry_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE translation_queue (
                        item_key TEXT PRIMARY KEY,
                        source_attachment_key TEXT NOT NULL,
                        state TEXT NOT NULL,
                        output_pdf TEXT NOT NULL,
                        last_error TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    )
                    """
                )
            snapshot = self.snapshot()
            snapshot.translation_queue.append(
                {
                    "parent_item_key": "AB12CD34",
                    "source_attachment_key": "EN12PDF3",
                    "status": "retry_wait",
                    "output_pdf": "",
                    "last_error": "Concurrency limit exceeded for account",
                    "attempt_count": "1",
                    "downloaded_at": "",
                    "next_attempt_at": "2026-08-20T12:10:00+00:00",
                    "observed_at": snapshot.observed_at,
                }
            )

            zotero_workflow.store_snapshot(database, snapshot)
            zotero_workflow.store_snapshot(database, snapshot)

            with sqlite3.connect(database) as connection:
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(translation_queue)"
                    ).fetchall()
                ]
                row = connection.execute(
                    "SELECT state, attempt_count, downloaded_at, next_attempt_at "
                    "FROM translation_queue WHERE item_key='AB12CD34'"
                ).fetchone()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(columns.count("attempt_count"), 1)
        self.assertEqual(columns.count("downloaded_at"), 1)
        self.assertEqual(columns.count("next_attempt_at"), 1)
        self.assertEqual(
            row,
            ("retry_wait", 1, "", "2026-08-20T12:10:00+00:00"),
        )
        self.assertEqual(version, zotero_workflow.SCHEMA_VERSION)

    def test_shared_database_connect_also_migrates_translation_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE translation_queue (
                        item_key TEXT PRIMARY KEY,
                        source_attachment_key TEXT NOT NULL,
                        state TEXT NOT NULL,
                        output_pdf TEXT NOT NULL,
                        last_error TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    )
                    """
                )

            with workflow_database.connect(database) as connection:
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(translation_queue)"
                    ).fetchall()
                ]
                version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertIn("attempt_count", columns)
        self.assertIn("downloaded_at", columns)
        self.assertIn("next_attempt_at", columns)
        self.assertEqual(version, workflow_database.SCHEMA_VERSION)

    def test_later_sync_marks_removed_item_out_of_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            zotero_workflow.store_snapshot(database, self.snapshot())
            empty = zotero_workflow.WorkflowSnapshot(
                observed_at="2026-08-21T12:00:00+00:00",
                roots=self.snapshot().roots,
                items=[],
                memberships=[],
                attachments=[],
                mineru_documents=[],
                qmd_documents=[],
                translation_queue=[],
                pdf2zh_tasks=[],
                system_health=[],
            )
            zotero_workflow.store_snapshot(database, empty)
            with sqlite3.connect(database) as connection:
                in_scope = connection.execute(
                    "SELECT in_scope FROM items WHERE item_key='AB12CD34'"
                ).fetchone()[0]
            self.assertEqual(in_scope, 0)

    def test_export_csv_writes_one_row_per_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            output = Path(tmp) / "status.csv"
            zotero_workflow.store_snapshot(database, self.snapshot())
            count = zotero_workflow.export_csv(database, output)
            with output.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(count, 1)
            self.assertEqual(rows[0]["source_attachment_key"], "EN12PDF3")
            self.assertEqual(rows[0]["translation_attachment_key"], "CN34PDF5")
            self.assertEqual(rows[0]["collection_paths"], "Senescence")

    def test_collection_reference_distinguishes_key_name_and_path(self):
        self.assertEqual(
            zotero_workflow.collection_reference("XM2WM4D4"),
            {"key": "XM2WM4D4"},
        )
        self.assertEqual(
            zotero_workflow.collection_reference("Journal Club"),
            {"name": "Journal Club"},
        )
        self.assertEqual(
            zotero_workflow.collection_reference("Projects > Glioma"),
            {"path": "Projects > Glioma"},
        )

    def test_next_batch_uses_direct_membership_gate_order_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            snapshot = self.snapshot()
            snapshot.items[0]["date_added"] = "2026-08-20T12:00:00Z"

            early = copy.deepcopy(snapshot.items[0])
            early.update(
                item_key="AA11AA11",
                title="Earlier reviewable paper",
                date_added="2026-08-20T10:00:00Z",
                source_attachment_key="AA11PDF1",
            )
            blocked = copy.deepcopy(snapshot.items[0])
            blocked.update(
                item_key="BB22BB22",
                title="Blocked paper",
                date_added="2026-08-20T09:00:00Z",
                source_attachment_key=None,
                mineru_state="missing",
                qmd_state="missing",
            )
            child_only = copy.deepcopy(snapshot.items[0])
            child_only.update(
                item_key="CC33CC33",
                title="Already classified paper",
                date_added="2026-08-20T08:00:00Z",
                source_attachment_key="CC33PDF3",
            )
            snapshot.items.extend([early, blocked, child_only])
            snapshot.memberships.extend(
                [
                    {
                        "item_key": "AA11AA11",
                        "collection_key": "XM2WM4D4",
                        "collection_path": "Senescence",
                    },
                    {
                        "item_key": "BB22BB22",
                        "collection_key": "XM2WM4D4",
                        "collection_path": "Senescence",
                    },
                    {
                        "item_key": "CC33CC33",
                        "collection_key": "CH11LD22",
                        "collection_path": "Senescence > Models",
                    },
                ]
            )
            zotero_workflow.store_snapshot(database, snapshot)

            data = zotero_workflow.next_batch_data(database, "Senescence", limit=1)

            self.assertEqual(data["collection"]["collection_key"], "XM2WM4D4")
            self.assertEqual(data["direct_items"], 3)
            self.assertEqual(data["reviewable_items"], 2)
            self.assertEqual(data["blocked_items"], 1)
            self.assertEqual(data["selected_items"], 1)
            self.assertEqual(data["items"][0]["item_key"], "AA11AA11")

    def test_next_batch_rejects_untracked_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "workflow.sqlite3"
            zotero_workflow.store_snapshot(database, self.snapshot())
            with self.assertRaisesRegex(
                zotero_workflow.WorkflowError, "tracked collection not found"
            ):
                zotero_workflow.next_batch_data(database, "Missing")

    def test_supplementary_detection_does_not_classify_main_pdf(self):
        self.assertTrue(zotero_workflow._is_supplementary("SI", "paper.pdf"))
        self.assertTrue(
            zotero_workflow._is_supplementary("PDF", "paper_supplementary.pdf")
        )
        self.assertFalse(zotero_workflow._is_supplementary("PDF", "paper.pdf"))


if __name__ == "__main__":
    unittest.main()
