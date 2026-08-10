import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from zotero_mcp import mineru_client


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content=b""):
        self.payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        return self.payload

    def iter_content(self, chunk_size=1024):
        del chunk_size
        yield self.content


class MinerUClientTests(unittest.TestCase):
    def mineru_zip(self, *, include_model=True):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("full.md", "# Paper")
            archive.writestr("abc_content_list.json", "[]")
            archive.writestr("abc_content_list_v2.json", "[]")
            if include_model:
                archive.writestr("abc_model.json", "[]")
            archive.writestr("layout.json", "[]")
            archive.writestr("abc_origin.pdf", "%PDF-test")
            archive.writestr("images/figure.jpg", "image")
        return buffer.getvalue()

    def test_load_token_from_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("secret-token\n", encoding="utf-8")
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(mineru_client.load_token(path), "secret-token")

    def test_submit_file_uses_batch_upload_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-test")
            post = FakeResponse(
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {"batch_id": "B1", "file_urls": ["https://upload"]},
                }
            )
            put = FakeResponse(status_code=200)
            with (
                mock.patch.object(
                    mineru_client.requests, "post", return_value=post
                ) as post_call,
                mock.patch.object(mineru_client.requests, "put", return_value=put),
            ):
                result = mineru_client.submit_file(
                    pdf,
                    data_id="ITEM1",
                    page_ranges="1-5",
                    token="token",
                )
            body = post_call.call_args.kwargs["json"]
            self.assertEqual(body["model_version"], "vlm")
            self.assertEqual(body["language"], "en")
            self.assertEqual(body["files"][0]["data_id"], "ITEM1")
            self.assertEqual(body["files"][0]["page_ranges"], "1-5")
            self.assertEqual(result["batch_id"], "B1")

    def test_request_upload_batch_supports_multiple_files(self):
        post = FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "batch_id": "B2",
                    "file_urls": ["https://upload/1", "https://upload/2"],
                },
            }
        )
        specs = [
            {"name": "one.pdf", "data_id": "ITEM1"},
            {"name": "two.pdf", "data_id": "ITEM2"},
        ]
        with mock.patch.object(
            mineru_client.requests, "post", return_value=post
        ) as post_call:
            result = mineru_client.request_upload_batch(specs, token="token")
        self.assertEqual(result["batch_id"], "B2")
        self.assertEqual(len(result["file_urls"]), 2)
        self.assertEqual(post_call.call_args.kwargs["json"]["files"], specs)

    def test_request_upload_batch_rejects_more_than_fifty_files(self):
        specs = [{"name": f"{index}.pdf"} for index in range(51)]
        with self.assertRaisesRegex(mineru_client.MinerUError, "1-50 files"):
            mineru_client.request_upload_batch(specs, token="token")

    def test_extract_results_returns_entire_batch(self):
        results = [{"data_id": "ITEM1"}, {"data_id": "ITEM2"}]
        self.assertEqual(
            mineru_client.extract_results({"extract_result": results}), results
        )

    def test_safe_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../outside.txt", "bad")
            with self.assertRaises(mineru_client.MinerUError):
                mineru_client.safe_extract(zip_path, Path(tmp) / "out")

    def test_download_and_extract_reports_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = FakeResponse(content=self.mineru_zip())
            with mock.patch.object(
                mineru_client.requests, "get", return_value=response
            ):
                result = mineru_client.download_and_extract(
                    "https://example.test/result.zip", Path(tmp) / "result"
                )
            self.assertTrue(Path(result["full_md"]).is_file())
            self.assertTrue(Path(result["content_list_json"]).is_file())
            self.assertTrue(Path(result["content_list_v2_json"]).is_file())
            self.assertTrue(Path(result["middle_json"]).is_file())
            self.assertTrue(Path(result["model_json"]).is_file())
            self.assertTrue(Path(result["original_file"]).is_file())
            output_dir = Path(result["output_dir"])
            self.assertTrue((output_dir / "images" / "figure.jpg").is_file())
            self.assertFalse((output_dir / "abc_content_list.json").exists())
            self.assertFalse((output_dir / "abc_model.json").exists())

    def test_download_replaces_incomplete_result_only_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "result"
            output_dir.mkdir()
            (output_dir / "full.md").write_text("partial", encoding="utf-8")
            response = FakeResponse(content=self.mineru_zip())
            with mock.patch.object(
                mineru_client.requests, "get", return_value=response
            ):
                mineru_client.download_and_extract(
                    "https://example.test/result.zip", output_dir
                )
            self.assertEqual(mineru_client.missing_result_artifacts(output_dir), [])
            self.assertEqual(
                (output_dir / "full.md").read_text(encoding="utf-8"), "# Paper"
            )

    def test_incomplete_download_keeps_existing_result_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "result"
            output_dir.mkdir()
            (output_dir / "full.md").write_text("old partial", encoding="utf-8")
            response = FakeResponse(content=self.mineru_zip(include_model=False))
            with (
                mock.patch.object(mineru_client.requests, "get", return_value=response),
                self.assertRaisesRegex(mineru_client.MinerUError, "incomplete"),
            ):
                mineru_client.download_and_extract(
                    "https://example.test/result.zip", output_dir
                )
            self.assertEqual(
                (output_dir / "full.md").read_text(encoding="utf-8"),
                "old partial",
            )
            self.assertFalse((output_dir / "result.zip").exists())

    def test_item_directory_name_is_zotero_key(self):
        item = {
            "data": {
                "key": "ABCD1234",
                "title": "Single-cell Immune Aging",
                "date": "2026-01-01",
                "creators": [{"lastName": "Wang"}],
            }
        }
        self.assertEqual(
            mineru_client.item_directory_name(item),
            "ABCD1234",
        )

    def test_default_result_dir_omits_batch_id(self):
        item = {"data": {"key": "ITEM1"}}
        with mock.patch.object(
            mineru_client, "DEFAULT_OUTPUT_ROOT", Path("/tmp/mineru")
        ):
            self.assertEqual(
                mineru_client.default_result_dir(item), Path("/tmp/mineru/ITEM1")
            )

    def test_find_local_result_uses_item_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_dir = root / "ITEM1"
            item_dir.mkdir(parents=True)
            for name in mineru_client.REQUIRED_RESULT_ARTIFACTS:
                (item_dir / name).write_bytes(b"result")
            with mock.patch.object(mineru_client, "DEFAULT_OUTPUT_ROOT", root):
                result = mineru_client.find_local_result("ITEM1")
            self.assertEqual(Path(result["full_md"]).parent, item_dir.resolve())

    def test_find_local_result_rejects_partial_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_dir = root / "ITEM1"
            item_dir.mkdir(parents=True)
            (item_dir / "full.md").write_text("partial", encoding="utf-8")
            with mock.patch.object(mineru_client, "DEFAULT_OUTPUT_ROOT", root):
                result = mineru_client.find_local_result("ITEM1")
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
