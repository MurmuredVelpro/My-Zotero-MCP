import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zotero_mcp import mineru_qmd_pipeline


class FakeProcess:
    def __init__(self, return_code=0):
        self.return_code = return_code
        self.pid = 12345

    def wait(self, timeout=None):
        return self.return_code

    def poll(self):
        return self.return_code


class MinerUQmdPipelineTests(unittest.TestCase):
    def write_state(self, state_dir, batch_id, **fields):
        state_dir.mkdir(parents=True, exist_ok=True)
        state = {"batch_id": batch_id, "files": [], **fields}
        (state_dir / f"{batch_id}.json").write_text(json.dumps(state), encoding="utf-8")

    def test_find_active_batch_recovers_one_unfinished_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            self.write_state(
                state_dir,
                "DONE",
                collection_key="COLL",
                status="complete",
            )
            self.write_state(
                state_dir,
                "ACTIVE",
                collection_key="COLL",
                status="processing",
            )
            self.assertEqual(
                mineru_qmd_pipeline.find_active_batch("COLL", state_dir),
                "ACTIVE",
            )

    def test_find_active_batch_rejects_multiple_unfinished_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            for batch_id in ("ONE", "TWO"):
                self.write_state(
                    state_dir,
                    batch_id,
                    collection_key="COLL",
                    status="uploaded",
                )
            with self.assertRaisesRegex(RuntimeError, "Multiple unfinished"):
                mineru_qmd_pipeline.find_active_batch("COLL", state_dir)

    def test_failed_item_requires_explicit_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            self.write_state(
                state_dir,
                "FAILED",
                collection_key="COLL",
                status="complete",
                files=[{"data_id": "ITEM1", "result_state": "failed"}],
            )
            self.assertEqual(
                mineru_qmd_pipeline.unresolved_failed_keys(
                    "COLL", {"ITEM1", "ITEM2"}, state_dir
                ),
                ["ITEM1"],
            )

    def test_upload_failure_also_requires_explicit_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            self.write_state(
                state_dir,
                "FAILED",
                collection_key="COLL",
                status="partial-upload",
                files=[{"data_id": "ITEM1", "upload_error": "timeout"}],
            )
            self.assertEqual(
                mineru_qmd_pipeline.unresolved_failed_keys(
                    "COLL", {"ITEM1"}, state_dir
                ),
                ["ITEM1"],
            )

    def test_selection_honors_total_and_per_batch_page_limits(self):
        plan = {
            "ready": [
                {"data_id": "A", "pages": 30},
                {"data_id": "B", "pages": 40},
                {"data_id": "C", "pages": 50},
            ]
        }
        selected = mineru_qmd_pipeline.select_pipeline_batch(
            plan,
            max_files=50,
            max_pages_per_batch=100,
            remaining_page_budget=70,
        )
        self.assertEqual([record["data_id"] for record in selected], ["A", "B"])

    def test_submit_batch_uses_preselected_records(self):
        selected = [{"data_id": "ITEM1", "pages": 10}]
        with mock.patch.object(
            mineru_qmd_pipeline.mineru_workflow,
            "submit_selected_batch",
            return_value=({"batch_id": "BATCH1"}, []),
        ) as submit:
            batch_id = mineru_qmd_pipeline.submit_batch(
                "COLLECTION1",
                selected,
                deferred_count=2,
                max_pages=10000,
            )
        self.assertEqual(batch_id, "BATCH1")
        submit.assert_called_once_with(
            "COLLECTION1",
            selected,
            deferred_count=2,
            max_pages=10000,
        )

    def test_start_qmd_cycle_serializes_update_before_embed(self):
        previous = FakeProcess()
        with (
            mock.patch.object(mineru_qmd_pipeline, "wait_qmd_embed") as wait_embed,
            mock.patch.object(mineru_qmd_pipeline, "run_qmd_update") as update,
            mock.patch.object(
                mineru_qmd_pipeline.mineru_workflow,
                "todo_keys_needing_qmd",
                return_value=["ITEM1"],
            ),
            mock.patch.object(
                mineru_qmd_pipeline,
                "start_qmd_embed",
                return_value="next",
            ) as start_embed,
        ):
            result = mineru_qmd_pipeline.start_qmd_cycle(previous, "BATCH1")
        self.assertEqual(result, "next")
        wait_embed.assert_called_once_with(previous)
        update.assert_called_once_with()
        start_embed.assert_called_once_with("BATCH1", ["ITEM1"])

    def test_start_qmd_cycle_skips_when_nothing_needs_indexing(self):
        with (
            mock.patch.object(mineru_qmd_pipeline, "wait_qmd_embed"),
            mock.patch.object(mineru_qmd_pipeline, "run_qmd_update") as update,
            mock.patch.object(
                mineru_qmd_pipeline.mineru_workflow,
                "todo_keys_needing_qmd",
                return_value=[],
            ),
            mock.patch.object(mineru_qmd_pipeline, "start_qmd_embed") as embed,
        ):
            result = mineru_qmd_pipeline.start_qmd_cycle(None, "startup")
        self.assertIsNone(result)
        update.assert_not_called()
        embed.assert_not_called()

    def test_successful_embed_marks_parsed_todo_rows_indexed(self):
        process = FakeProcess(return_code=0)
        process.qmd_item_keys = ["ITEM1"]
        with (
            mock.patch.object(
                mineru_qmd_pipeline,
                "verify_qmd_items",
                return_value=(["ITEM1"], []),
            ),
            mock.patch.object(
                mineru_qmd_pipeline.mineru_workflow,
                "mark_todo_qmd_indexed",
            ) as mark_indexed,
        ):
            mineru_qmd_pipeline.wait_qmd_embed(process)
        mark_indexed.assert_called_once_with(["ITEM1"])

    def test_embed_does_not_mark_unreadable_qmd_document(self):
        process = FakeProcess(return_code=0)
        process.qmd_item_keys = ["ITEM1", "ITEM2"]
        with (
            mock.patch.object(
                mineru_qmd_pipeline,
                "verify_qmd_items",
                return_value=(["ITEM1"], ["ITEM2"]),
            ),
            mock.patch.object(
                mineru_qmd_pipeline.mineru_workflow,
                "mark_todo_qmd_indexed",
            ) as mark_indexed,
            self.assertRaisesRegex(RuntimeError, "ITEM2"),
        ):
            mineru_qmd_pipeline.wait_qmd_embed(process)
        mark_indexed.assert_called_once_with(["ITEM1"])

    def test_qmd_verification_uses_one_multi_get_process(self):
        result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"file": "qmd://zotero-mineru/ITEM1/full.md"}]),
            stderr="",
        )
        with (
            mock.patch.object(mineru_qmd_pipeline, "qmd_command", return_value="qmd"),
            mock.patch.object(
                mineru_qmd_pipeline,
                "qmd_collection",
                return_value="zotero-mineru",
            ),
            mock.patch.object(
                mineru_qmd_pipeline.subprocess, "run", return_value=result
            ) as run,
        ):
            verified, missing = mineru_qmd_pipeline.verify_qmd_items(["ITEM1", "ITEM2"])
        self.assertEqual(verified, ["ITEM1"])
        self.assertEqual(missing, ["ITEM2"])
        run.assert_called_once()
        self.assertIn("multi-get", run.call_args.args[0])

    def test_qmd_embed_captures_pending_keys_at_start(self):
        process = FakeProcess(return_code=0)
        with (
            mock.patch.object(
                mineru_qmd_pipeline.mineru_workflow,
                "todo_keys_needing_qmd",
                return_value=["ITEM1"],
            ),
            mock.patch.object(
                mineru_qmd_pipeline.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
            mock.patch.object(
                mineru_qmd_pipeline,
                "qmd_command",
                return_value="qmd",
            ),
            mock.patch.object(
                mineru_qmd_pipeline,
                "qmd_collection",
                return_value="zotero-mineru",
            ),
        ):
            result = mineru_qmd_pipeline.start_qmd_embed("BATCH1")
        self.assertIs(result, process)
        self.assertEqual(process.qmd_item_keys, ["ITEM1"])
        command = popen.call_args.args[0]
        self.assertEqual(command, ["qmd", "embed", "-c", "zotero-mineru"])
        self.assertNotIn("--max-docs-per-batch", command)

    def test_collect_failure_reports_batch_and_failed_items(self):
        state = {
            "batch_id": "BATCH1",
            "files": [{"data_id": "ITEM1", "result_state": "failed"}],
        }
        with (
            mock.patch.object(
                mineru_qmd_pipeline.mineru_workflow,
                "collect_batch",
                side_effect=RuntimeError("MINERU_COLLECT failed with exit code 1"),
            ),
            mock.patch.object(
                mineru_qmd_pipeline.mineru_workflow,
                "load_state",
                return_value=state,
            ),
            self.assertRaisesRegex(RuntimeError, "batch BATCH1.*ITEM1"),
        ):
            mineru_qmd_pipeline.collect_once("BATCH1")

    def test_validate_arguments_accepts_bounded_defaults(self):
        arguments = SimpleNamespace(
            max_files=50,
            page_budget=1000,
            max_pages_per_batch=1000,
            max_batches=0,
            poll_seconds=60,
            max_wait_minutes=180,
        )
        with mock.patch.object(mineru_qmd_pipeline, "qmd_command", return_value="qmd"):
            mineru_qmd_pipeline.validate_arguments(arguments)

    def test_pipeline_lock_rejects_a_second_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "pipeline.lock"
            with (
                mineru_qmd_pipeline.pipeline_lock(lock_path),
                self.assertRaisesRegex(RuntimeError, "already running"),
                mineru_qmd_pipeline.pipeline_lock(lock_path),
            ):
                pass


if __name__ == "__main__":
    unittest.main()
