import csv
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zotero_mcp import mineru_workflow


class MinerUWorkflowTests(unittest.TestCase):
    def write_todo(self, path, rows):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=mineru_workflow.TODO_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def plan_single_pdf(self, pdf, attachment_key, tracked=None):
        item = {"data": {"key": "ITEM1", "title": "Paper"}}
        with (
            mock.patch.object(
                mineru_workflow.zotero_local,
                "fetch_all_collections",
                return_value=[],
            ),
            mock.patch.object(
                mineru_workflow.zotero_local, "collection_index", return_value={}
            ),
            mock.patch.object(
                mineru_workflow.zotero_local,
                "collect_collection_items",
                return_value=([item], {}),
            ),
            mock.patch.object(
                mineru_workflow, "todo_rows_by_key", return_value=tracked or {}
            ),
            mock.patch.object(
                mineru_workflow.zotero_local,
                "pdf_attachments_for_item",
                return_value=[],
            ),
            mock.patch.object(
                mineru_workflow.zotero_local,
                "english_pdf_attachment_for_item",
                return_value={"key": attachment_key, "path": pdf},
            ),
            mock.patch.object(
                mineru_workflow.mineru_client,
                "find_local_result",
                return_value={"full_md": "/tmp/ITEM1/full.md"},
            ),
            mock.patch.object(mineru_workflow, "pdf_page_count", return_value=10),
        ):
            return mineru_workflow.collection_plan("COLLECTION1")

    def test_collection_plan_forwards_recursive_scope(self):
        with (
            mock.patch.object(
                mineru_workflow.zotero_local,
                "fetch_all_collections",
                return_value=[],
            ),
            mock.patch.object(
                mineru_workflow.zotero_local,
                "collection_index",
                return_value={},
            ),
            mock.patch.object(
                mineru_workflow.zotero_local,
                "collect_collection_items",
                return_value=([], {}),
            ) as collect_items,
            mock.patch.object(mineru_workflow, "todo_rows_by_key", return_value={}),
        ):
            mineru_workflow.collection_plan("COLLECTION1", recursive=True)
        collect_items.assert_called_once_with("COLLECTION1", {}, True, 1000)

    def test_state_is_atomic_private_and_loadable_as_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".jobs"
            path = mineru_workflow.save_state(
                {"batch_id": "BATCH1", "files": []}, state_dir
            )
            self.assertEqual(path, state_dir / "BATCH1.json")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                mineru_workflow.load_state("latest", state_dir)["batch_id"],
                "BATCH1",
            )
            self.assertFalse(list(state_dir.glob("*.tmp")))

    def test_select_batch_honors_file_and_page_limits(self):
        ready = [
            {"data_id": "A", "pages": 30},
            {"data_id": "B", "pages": 40},
            {"data_id": "C", "pages": 50},
        ]
        selected, deferred = mineru_workflow.select_batch(
            ready, max_files=50, max_pages=70
        )
        self.assertEqual([record["data_id"] for record in selected], ["A", "B"])
        self.assertEqual([record["data_id"] for record in deferred], ["C"])

    def test_collection_plan_marks_replaced_attachment_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "current.pdf"
            pdf.write_bytes(b"%PDF-current")
            plan = self.plan_single_pdf(
                pdf,
                "NEWPDF1",
                {
                    "ITEM1": {
                        "parsed_attachment_key": "OLDPDF1",
                        "mineru_parsed": "true",
                    }
                },
            )
            self.assertEqual(len(plan["ready"]), 1)
            self.assertEqual(plan["ready"][0]["plan_status"], "stale")
            self.assertEqual(plan["ready"][0]["attachment_key"], "NEWPDF1")
            self.assertEqual(plan["ready"][0]["parsed_attachment_key"], "OLDPDF1")

    def test_collection_plan_reparses_untracked_local_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "current.pdf"
            pdf.write_bytes(b"%PDF-current")
            plan = self.plan_single_pdf(pdf, "PDF1")
            self.assertEqual(plan["existing"], [])
            self.assertEqual(plan["ready"][0]["plan_status"], "untracked")
            self.assertTrue(plan["ready"][0]["replace_existing"])

    def test_todo_status_transitions_preserve_old_key_until_parse_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            todo_path = Path(tmp) / "todo.csv"
            self.write_todo(
                todo_path,
                [
                    {
                        "item_key": "ITEM1",
                        "parsed_attachment_key": "OLDPDF1",
                        "has_pdf": "true",
                        "mineru_parsed": "true",
                        "qmd_indexed": "true",
                    }
                ],
            )
            mineru_workflow.mark_todo_stale("ITEM1", todo_path)
            stale = mineru_workflow.todo_rows_by_key(todo_path)["ITEM1"]
            self.assertEqual(stale["parsed_attachment_key"], "OLDPDF1")
            self.assertEqual(stale["mineru_parsed"], "false")
            self.assertEqual(stale["qmd_indexed"], "false")

            mineru_workflow.mark_todo_parsed("ITEM1", "NEWPDF1", todo_path)
            parsed = mineru_workflow.todo_rows_by_key(todo_path)["ITEM1"]
            self.assertEqual(parsed["parsed_attachment_key"], "NEWPDF1")
            self.assertEqual(parsed["mineru_parsed"], "true")
            self.assertEqual(parsed["qmd_indexed"], "false")

            mineru_workflow.mark_todo_qmd_indexed(todo_path=todo_path)
            indexed = mineru_workflow.todo_rows_by_key(todo_path)["ITEM1"]
            self.assertEqual(indexed["qmd_indexed"], "true")

    def test_concurrent_todo_updates_preserve_both_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            todo_path = Path(tmp) / "todo.csv"
            self.write_todo(
                todo_path,
                [
                    {
                        "item_key": key,
                        "parsed_attachment_key": "",
                        "has_pdf": "true",
                        "mineru_parsed": "false",
                        "qmd_indexed": "false",
                    }
                    for key in ("ITEM1", "ITEM2")
                ],
            )
            original_save = mineru_workflow._save_todo_rows_unlocked
            first_save = threading.Event()
            release_first = threading.Event()
            save_calls = 0
            save_calls_lock = threading.Lock()

            def delayed_save(rows, path=None):
                nonlocal save_calls
                with save_calls_lock:
                    save_calls += 1
                    is_first = save_calls == 1
                if is_first:
                    first_save.set()
                    self.assertTrue(release_first.wait(timeout=5))
                return original_save(rows, path)

            def update(item_key):
                mineru_workflow.update_todo_row(
                    item_key, {"mineru_parsed": "true"}, todo_path
                )

            with mock.patch.object(
                mineru_workflow,
                "_save_todo_rows_unlocked",
                side_effect=delayed_save,
            ):
                first = threading.Thread(target=update, args=("ITEM1",))
                second = threading.Thread(target=update, args=("ITEM2",))
                first.start()
                self.assertTrue(first_save.wait(timeout=5))
                second.start()
                time.sleep(0.05)
                release_first.set()
                first.join(timeout=5)
                second.join(timeout=5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            rows = mineru_workflow.todo_rows_by_key(todo_path)
            self.assertEqual(rows["ITEM1"]["mineru_parsed"], "true")
            self.assertEqual(rows["ITEM2"]["mineru_parsed"], "true")

    def test_sync_todo_adds_new_parent_items_from_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            todo_path = Path(tmp) / "todo.csv"
            self.write_todo(todo_path, [])
            plan = {
                "existing": [],
                "ready": [{"data_id": "NEWPDF"}],
                "blocked": [],
                "missing": [{"data_id": "NOPDF"}],
            }
            added = mineru_workflow.sync_todo_from_plan(plan, todo_path)
            self.assertEqual(added, ["NEWPDF", "NOPDF"])
            rows = mineru_workflow.todo_rows_by_key(todo_path)
            self.assertEqual(rows["NEWPDF"]["has_pdf"], "true")
            self.assertEqual(rows["NOPDF"]["has_pdf"], "false")
            self.assertEqual(rows["NEWPDF"]["parsed_attachment_key"], "")

    def test_submit_batch_saves_receipt_before_uploading(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".jobs"
            todo_path = Path(tmp) / "todo.csv"
            self.write_todo(todo_path, [])
            plan = {
                "collection_key": "COLLECTION1",
                "item_count": 1,
                "ready_pages": 10,
                "ready": [
                    {
                        "data_id": "ITEM1",
                        "title": "Paper",
                        "path": "/tmp/paper.pdf",
                        "name": "paper.pdf",
                        "pages": 10,
                        "size_bytes": 100,
                    }
                ],
                "existing": [],
                "missing": [],
                "blocked": [],
            }
            observed = {}

            def inspect_saved_state(state):
                path = state_dir / "BATCH1.json"
                observed.update(json.loads(path.read_text(encoding="utf-8")))
                return []

            arguments = SimpleNamespace(
                collection_key="COLLECTION1",
                max_files=50,
                max_pages=100,
                recursive=False,
            )
            with (
                mock.patch.object(mineru_workflow, "STATE_DIR", state_dir),
                mock.patch.object(mineru_workflow, "TODO_PATH", todo_path),
                mock.patch.object(
                    mineru_workflow, "collection_plan", return_value=plan
                ),
                mock.patch.object(
                    mineru_workflow.mineru_client,
                    "request_upload_batch",
                    return_value={
                        "batch_id": "BATCH1",
                        "file_urls": ["https://upload/1"],
                    },
                ),
                mock.patch.object(
                    mineru_workflow,
                    "upload_pending",
                    side_effect=inspect_saved_state,
                ),
            ):
                result = mineru_workflow.cmd_submit_batch(arguments)
            self.assertEqual(result, 0)
            self.assertEqual(observed["batch_id"], "BATCH1")
            self.assertFalse(observed["files"][0]["uploaded"])

    def test_collect_downloads_every_done_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "Zotero_MinerU"
            state_dir = output_root / ".jobs"
            state = {
                "batch_id": "BATCH1",
                "files": [
                    {
                        "data_id": "ITEM1",
                        "name": "one.pdf",
                        "path": "/tmp/one.pdf",
                        "uploaded": True,
                        "downloaded": False,
                    },
                    {
                        "data_id": "ITEM2",
                        "name": "two.pdf",
                        "path": "/tmp/two.pdf",
                        "uploaded": True,
                        "downloaded": False,
                    },
                ],
            }
            with mock.patch.object(mineru_workflow, "STATE_DIR", state_dir):
                mineru_workflow.save_state(state)
            batch = {
                "extract_result": [
                    {
                        "data_id": "ITEM1",
                        "file_name": "one.pdf",
                        "state": "done",
                        "full_zip_url": "https://result/one.zip",
                    },
                    {
                        "data_id": "ITEM2",
                        "file_name": "two.pdf",
                        "state": "done",
                        "full_zip_url": "https://result/two.zip",
                    },
                ]
            }
            arguments = SimpleNamespace(batch_id="BATCH1")
            with (
                mock.patch.object(mineru_workflow, "OUTPUT_ROOT", output_root),
                mock.patch.object(mineru_workflow, "STATE_DIR", state_dir),
                mock.patch.object(mineru_workflow, "upload_pending", return_value=[]),
                mock.patch.object(
                    mineru_workflow.mineru_client,
                    "get_batch",
                    return_value=batch,
                ),
                mock.patch.object(
                    mineru_workflow.mineru_client, "download_and_extract"
                ) as download,
                mock.patch.object(
                    mineru_workflow,
                    "verify_result_dir",
                    return_value={"artifact_count": 7, "broken_images": 0},
                ),
            ):
                result = mineru_workflow.cmd_collect(arguments)
            self.assertEqual(result, 0)
            self.assertEqual(download.call_count, 2)
            completed = mineru_workflow.load_state("BATCH1", state_dir)
            self.assertEqual(completed["status"], "complete")
            self.assertTrue(all(record["downloaded"] for record in completed["files"]))

    def test_collect_replaces_stale_result_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "Zotero_MinerU"
            state_dir = output_root / ".jobs"
            todo_path = output_root / "todo.csv"
            old_output = output_root / "ITEM1"
            old_output.mkdir(parents=True)
            (old_output / "version.txt").write_text("old", encoding="utf-8")
            self.write_todo(
                todo_path,
                [
                    {
                        "item_key": "ITEM1",
                        "parsed_attachment_key": "OLDPDF1",
                        "has_pdf": "true",
                        "mineru_parsed": "false",
                        "qmd_indexed": "false",
                    }
                ],
            )
            state = {
                "batch_id": "BATCH1",
                "files": [
                    {
                        "data_id": "ITEM1",
                        "attachment_key": "NEWPDF1",
                        "name": "paper.pdf",
                        "path": "/tmp/paper.pdf",
                        "replace_existing": True,
                        "uploaded": True,
                        "downloaded": False,
                    }
                ],
            }
            with mock.patch.object(mineru_workflow, "STATE_DIR", state_dir):
                mineru_workflow.save_state(state)

            def download_result(_url, destination):
                destination.mkdir(parents=True)
                (destination / "version.txt").write_text("new", encoding="utf-8")
                return {"output_dir": str(destination)}

            batch = {
                "extract_result": [
                    {
                        "data_id": "ITEM1",
                        "file_name": "paper.pdf",
                        "state": "done",
                        "full_zip_url": "https://result/paper.zip",
                    }
                ]
            }
            with (
                mock.patch.object(mineru_workflow, "OUTPUT_ROOT", output_root),
                mock.patch.object(mineru_workflow, "STATE_DIR", state_dir),
                mock.patch.object(mineru_workflow, "TODO_PATH", todo_path),
                mock.patch.object(mineru_workflow, "upload_pending", return_value=[]),
                mock.patch.object(
                    mineru_workflow.mineru_client, "get_batch", return_value=batch
                ),
                mock.patch.object(
                    mineru_workflow.mineru_client,
                    "download_and_extract",
                    side_effect=download_result,
                ),
                mock.patch.object(
                    mineru_workflow,
                    "verify_result_dir",
                    return_value={"artifact_count": 7, "broken_images": 0},
                ),
            ):
                result = mineru_workflow.cmd_collect(SimpleNamespace(batch_id="BATCH1"))

            self.assertEqual(result, 0)
            self.assertEqual(
                (output_root / "ITEM1" / "version.txt").read_text(encoding="utf-8"),
                "new",
            )
            self.assertEqual(list((state_dir / "BATCH1").iterdir()), [])
            row = mineru_workflow.todo_rows_by_key(todo_path)["ITEM1"]
            self.assertEqual(row["parsed_attachment_key"], "NEWPDF1")
            self.assertEqual(row["mineru_parsed"], "true")
            self.assertEqual(row["qmd_indexed"], "false")

    def test_collect_keeps_old_result_when_replacement_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "Zotero_MinerU"
            state_dir = output_root / ".jobs"
            old_output = output_root / "ITEM1"
            old_output.mkdir(parents=True)
            (old_output / "version.txt").write_text("old", encoding="utf-8")
            state = {
                "batch_id": "BATCH1",
                "files": [
                    {
                        "data_id": "ITEM1",
                        "name": "paper.pdf",
                        "path": "/tmp/paper.pdf",
                        "replace_existing": True,
                        "uploaded": True,
                        "downloaded": False,
                    }
                ],
            }
            with mock.patch.object(mineru_workflow, "STATE_DIR", state_dir):
                mineru_workflow.save_state(state)

            def download_result(_url, destination):
                destination.mkdir(parents=True)
                (destination / "version.txt").write_text("invalid", encoding="utf-8")

            batch = {
                "extract_result": [
                    {
                        "data_id": "ITEM1",
                        "file_name": "paper.pdf",
                        "state": "done",
                        "full_zip_url": "https://result/paper.zip",
                    }
                ]
            }
            with (
                mock.patch.object(mineru_workflow, "OUTPUT_ROOT", output_root),
                mock.patch.object(mineru_workflow, "STATE_DIR", state_dir),
                mock.patch.object(mineru_workflow, "upload_pending", return_value=[]),
                mock.patch.object(
                    mineru_workflow.mineru_client, "get_batch", return_value=batch
                ),
                mock.patch.object(
                    mineru_workflow.mineru_client,
                    "download_and_extract",
                    side_effect=download_result,
                ),
                mock.patch.object(
                    mineru_workflow,
                    "verify_result_dir",
                    side_effect=RuntimeError("invalid replacement"),
                ),
                self.assertRaisesRegex(RuntimeError, "invalid replacement"),
            ):
                mineru_workflow.cmd_collect(SimpleNamespace(batch_id="BATCH1"))

            self.assertEqual(
                (old_output / "version.txt").read_text(encoding="utf-8"), "old"
            )

    def test_replacement_restores_old_result_when_final_move_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "ITEM1"
            staged_dir = root / "staged"
            recovery_dir = root / "ITEM1.previous"
            output_dir.mkdir()
            staged_dir.mkdir()
            (output_dir / "version.txt").write_text("old", encoding="utf-8")
            (staged_dir / "version.txt").write_text("new", encoding="utf-8")
            original_replace = Path.replace

            def fail_staged_replace(path, target):
                if path == staged_dir:
                    raise OSError("simulated move failure")
                return original_replace(path, target)

            with (
                mock.patch.object(Path, "replace", new=fail_staged_replace),
                self.assertRaisesRegex(OSError, "simulated move failure"),
            ):
                mineru_workflow.mineru_client.install_result_directory(
                    staged_dir, output_dir, recovery_dir
                )
            self.assertEqual(
                (output_dir / "version.txt").read_text(encoding="utf-8"),
                "old",
            )
            self.assertFalse(recovery_dir.exists())

    def test_verify_result_dir_checks_canonical_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "ITEM1"
            (output_dir / "images").mkdir(parents=True)
            (output_dir / "full.md").write_text(
                "# Paper\n\n![figure](images/figure.png)\n", encoding="utf-8"
            )
            (output_dir / "images" / "figure.png").write_bytes(b"image")
            for name in (
                "content_list.json",
                "content_list_v2.json",
                "model.json",
                "layout.json",
            ):
                (output_dir / name).write_text(json.dumps([]), encoding="utf-8")
            (output_dir / "origin.pdf").write_bytes(b"%PDF-1.4\n")
            with zipfile.ZipFile(output_dir / "result.zip", "w") as archive:
                archive.writestr("full.md", "# Paper")
            result = mineru_workflow.verify_result_dir(output_dir)
            self.assertEqual(result["artifact_count"], 7)
            self.assertEqual(result["broken_images"], 0)


if __name__ == "__main__":
    unittest.main()
