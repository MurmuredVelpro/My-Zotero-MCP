import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zotero_mcp import zotero_extract


class ExtractDocumentTests(unittest.TestCase):
    def item(self):
        return {"key": "ITEM0001", "data": {"key": "ITEM0001", "title": "Paper"}}

    def attachment(self, pdf_path):
        return {"key": "PDF00001", "path": pdf_path}

    def test_current_mineru_full_md_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full_md = root / "full.md"
            full_md.write_text("# MinerU text\n", encoding="utf-8")
            output = root / "copy.txt"
            workflow = mock.Mock()
            workflow.tracked_result_status.return_value = "current"
            with (
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "english_pdf_attachment_for_item",
                    return_value=self.attachment(root / "paper.pdf"),
                ),
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "resolve_text_output_path",
                    return_value=output,
                ),
                mock.patch.object(
                    zotero_extract.mineru_client,
                    "find_local_result",
                    return_value={"full_md": str(full_md)},
                ),
                mock.patch.object(
                    zotero_extract,
                    "load_mineru_workflow",
                    return_value=workflow,
                ),
                mock.patch.object(zotero_extract.zotero_local, "run_command") as run,
            ):
                document = zotero_extract.extract_document(self.item())

            self.assertEqual(document.source, "mineru")
            self.assertEqual(document.attachment_key, "PDF00001")
            self.assertEqual(output.read_text(encoding="utf-8"), "# MinerU text\n")
            run.assert_not_called()

    def test_stale_mineru_result_falls_back_to_current_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            output = root / "paper.txt"

            def write_output(_command):
                output.write_text("Local text", encoding="utf-8")

            workflow = mock.Mock()
            workflow.tracked_result_status.return_value = "stale"
            with (
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "english_pdf_attachment_for_item",
                    return_value=self.attachment(pdf),
                ),
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "resolve_text_output_path",
                    return_value=output,
                ),
                mock.patch.object(
                    zotero_extract.mineru_client,
                    "find_local_result",
                    return_value={"full_md": str(root / "full.md")},
                ),
                mock.patch.object(
                    zotero_extract,
                    "load_mineru_workflow",
                    return_value=workflow,
                ),
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "pdf_tool_command",
                    return_value="pdftotext",
                ),
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "run_command",
                    side_effect=write_output,
                ) as run,
            ):
                document = zotero_extract.extract_document(self.item())

            self.assertEqual(document.source, "pdftotext")
            self.assertEqual(document.source_path, pdf)
            run.assert_called_once()

    def test_mineru_only_rejects_stale_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = mock.Mock()
            workflow.tracked_result_status.return_value = "stale"
            with (
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "english_pdf_attachment_for_item",
                    return_value=self.attachment(root / "paper.pdf"),
                ),
                mock.patch.object(
                    zotero_extract.zotero_local,
                    "resolve_text_output_path",
                    return_value=root / "paper.txt",
                ),
                mock.patch.object(
                    zotero_extract.mineru_client,
                    "find_local_result",
                    return_value={"full_md": str(root / "full.md")},
                ),
                mock.patch.object(
                    zotero_extract,
                    "load_mineru_workflow",
                    return_value=workflow,
                ),
                self.assertRaisesRegex(
                    zotero_extract.ExtractionError, "result status is stale"
                ),
            ):
                zotero_extract.extract_document(
                    self.item(), attachment_priority="mineru_only"
                )

    def test_missing_optional_mineru_workflow_does_not_break_module_import(self):
        with (
            mock.patch.object(zotero_extract, "_MINERU_WORKFLOW", None),
            mock.patch.object(
                zotero_extract.importlib,
                "import_module",
                side_effect=ModuleNotFoundError(
                    "No module named 'mineru_workflow'",
                    name="mineru_workflow",
                ),
            ),
            self.assertRaisesRegex(
                zotero_extract.MinerUWorkflowUnavailable,
                "workflow module is not installed",
            ),
        ):
            zotero_extract.load_mineru_workflow()


if __name__ == "__main__":
    unittest.main()
