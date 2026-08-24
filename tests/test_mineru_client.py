import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from zotero_mcp import mineru_client


class MinerUClientTests(unittest.TestCase):
    class StreamingResponse:
        def __init__(self, content, *, status_code=200, headers=None):
            self.content = content
            self.status_code = status_code
            self.headers = headers or {"Content-Length": str(len(content))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size):
            del chunk_size
            yield self.content[:3]
            yield self.content[3:]

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
            path = Path(tmp) / "mineru_api_token.secret"
            path.write_text("secret-token\n", encoding="utf-8")
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(mineru_client.load_token(path), "secret-token")

    def test_token_path_uses_secret_filename_by_default(self):
        expected = Path("/tmp/mineru/mineru_api_token.secret")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                mineru_client.zotero_runtime,
                "configured_path",
                return_value=None,
            ),
            mock.patch.object(
                mineru_client,
                "default_token_path",
                return_value=expected,
            ),
        ):
            self.assertEqual(mineru_client.token_path(), expected)

    def test_default_token_path_uses_independent_xdg_directory(self):
        with (
            mock.patch.object(mineru_client.os, "name", "posix"),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/config"}, clear=True),
        ):
            self.assertEqual(
                mineru_client.default_token_path(),
                Path("/tmp/config/mineru/mineru_api_token.secret"),
            )

    def test_submit_file_uses_batch_upload_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-test")
            client = mock.MagicMock()
            client.post.return_value = {
                "code": 0,
                "msg": "ok",
                "data": {"batch_id": "B1", "file_urls": ["https://upload"]},
            }
            uploaded = {}

            def capture_upload(_url, **kwargs):
                uploaded["data"] = kwargs["data"].read()
                return self.StreamingResponse(b"", status_code=200)

            with (
                mock.patch.object(mineru_client, "ApiClient", return_value=client),
                mock.patch.object(mineru_client, "load_token", return_value="token"),
                mock.patch.object(
                    mineru_client.requests,
                    "put",
                    side_effect=capture_upload,
                ) as put,
            ):
                result = mineru_client.submit_file(
                    pdf,
                    data_id="ITEM1",
                    page_ranges="1-5",
                    token="token",
                )
            body = client.post.call_args.kwargs["json"]
            self.assertEqual(body["model_version"], "vlm")
            self.assertEqual(body["language"], "en")
            self.assertEqual(body["files"][0]["data_id"], "ITEM1")
            self.assertEqual(body["files"][0]["page_ranges"], "1-5")
            self.assertEqual(result["batch_id"], "B1")
            put.assert_called_once()
            self.assertEqual(put.call_args.args[0], "https://upload")
            self.assertEqual(uploaded["data"], b"%PDF-test")

    def test_request_upload_batch_supports_multiple_files(self):
        client = mock.MagicMock()
        client.post.return_value = {
            "code": 0,
            "msg": "ok",
            "data": {
                "batch_id": "B2",
                "file_urls": ["https://upload/1", "https://upload/2"],
            },
        }
        specs = [
            {"name": "one.pdf", "data_id": "ITEM1"},
            {"name": "two.pdf", "data_id": "ITEM2"},
        ]
        with mock.patch.object(mineru_client, "ApiClient", return_value=client):
            result = mineru_client.request_upload_batch(specs, token="token")
        self.assertEqual(result["batch_id"], "B2")
        self.assertEqual(len(result["file_urls"]), 2)
        self.assertEqual(client.post.call_args.kwargs["json"]["files"], specs)

    def test_upload_rejects_http_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-test")
            with (
                mock.patch.object(
                    mineru_client.requests,
                    "put",
                    return_value=self.StreamingResponse(b"", status_code=500),
                ),
                self.assertRaisesRegex(mineru_client.MinerUError, "Upload PDF"),
            ):
                mineru_client.upload_file(pdf, "https://upload")

    def test_get_batch_preserves_raw_data_id_contract(self):
        client = mock.MagicMock()
        client.get.return_value = {
            "data": {
                "extract_result": [
                    {"data_id": "ITEM1", "file_name": "paper.pdf", "state": "done"}
                ]
            }
        }
        with mock.patch.object(mineru_client, "ApiClient", return_value=client):
            result = mineru_client.get_batch("B1", token="token")
        self.assertEqual(result["extract_result"][0]["data_id"], "ITEM1")
        client.get.assert_called_once_with("/extract-results/batch/B1")

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
            content = self.mineru_zip()
            with (
                mock.patch.object(
                    mineru_client.requests,
                    "get",
                    return_value=self.StreamingResponse(content),
                ) as get,
            ):
                result = mineru_client.download_and_extract(
                    "https://example.test/result.zip", Path(tmp) / "result"
                )
            get.assert_called_once()
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
            content = self.mineru_zip()
            with (
                mock.patch.object(
                    mineru_client.requests,
                    "get",
                    return_value=self.StreamingResponse(content),
                ),
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
            content = self.mineru_zip(include_model=False)
            with (
                mock.patch.object(
                    mineru_client.requests,
                    "get",
                    return_value=self.StreamingResponse(content),
                ),
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

    def test_truncated_stream_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "result"
            response = self.StreamingResponse(
                self.mineru_zip(),
                headers={"Content-Length": "999999"},
            )
            with (
                mock.patch.object(mineru_client.requests, "get", return_value=response),
                self.assertRaisesRegex(mineru_client.MinerUError, "incomplete"),
            ):
                mineru_client.download_and_extract(
                    "https://example.test/result.zip", output_dir
                )
            self.assertFalse(output_dir.exists())

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
