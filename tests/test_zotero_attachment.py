import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from zotero_mcp import zotero_attachment


class FakeResponse:
    def __init__(self, status_code=200, data=b""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.data = data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.auth = None
        self.puts = []

    def put(self, url, data, headers=None, timeout=None):
        payload = data.read() if hasattr(data, "read") else data
        self.puts.append((url, payload, headers, timeout))
        return FakeResponse()

    def delete(self, url, timeout=None):
        return FakeResponse(204)


class FakeApi:
    USER_ID = 123
    PARENT_KEY = "PAPER001"
    ATTACHMENT_KEY = "ATTACH01"

    def __init__(self, children=None):
        self.payload = None
        self.children = children or []

    def web_api_get_item(self, user_id, key):
        if key == self.PARENT_KEY:
            return {
                "key": key,
                "version": 1,
                "data": {"key": key, "itemType": "journalArticle"},
            }
        data = dict(self.payload)
        data["key"] = self.ATTACHMENT_KEY
        return {"key": self.ATTACHMENT_KEY, "version": 2, "data": data}

    def web_api_request_json(self, method, path, **kwargs):
        if method == "GET" and path.endswith("/children"):
            return self.children
        self.payload = dict(kwargs["payload"][0])
        return {"successful": {"0": {"key": self.ATTACHMENT_KEY, "version": 2}}}

    def web_api_request(self, method, path, **kwargs):
        return FakeResponse(204)


class AttachmentTests(unittest.TestCase):
    def test_import_uses_explicit_title_and_filename_for_metadata_and_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "staging-name.pdf"
            source.write_bytes(b"%PDF-1.4\ncontent")
            api = FakeApi()
            session = FakeSession()
            client = zotero_attachment.ZoteroAttachmentClient(
                config_loader=lambda environ: (
                    "https://dav.example/zotero/",
                    "user",
                    "pass",
                    30.0,
                    "test",
                ),
                session=session,
                api=api,
            )
            result = client.import_pdf(
                api.USER_ID,
                api.PARENT_KEY,
                source,
                "PDF",
                "2025_Journal_Title_Author.pdf",
            )

        self.assertEqual(api.payload["title"], "PDF")
        self.assertEqual(api.payload["filename"], "2025_Journal_Title_Author.pdf")
        self.assertEqual(result["filename"], "2025_Journal_Title_Author.pdf")
        archive_bytes = session.puts[0][1]
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(archive.namelist(), ["2025_Journal_Title_Author.pdf"])
            self.assertEqual(
                hashlib.md5(archive.read("2025_Journal_Title_Author.pdf")).hexdigest(),
                api.payload["md5"],
            )

    def test_retry_matches_parent_filename_and_md5_even_when_mtime_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "download.pdf"
            source.write_bytes(b"%PDF-1.4\ncontent")
            md5_hex = hashlib.md5(source.read_bytes()).hexdigest()
            existing = {
                "key": "ATTACH01",
                "data": {
                    "key": "ATTACH01",
                    "parentItem": "PAPER001",
                    "linkMode": "imported_file",
                    "contentType": "application/pdf",
                    "title": "PDF",
                    "filename": "final.pdf",
                    "md5": md5_hex,
                    "mtime": 123,
                },
            }
            api = FakeApi(children=[existing])
            session = FakeSession()
            client = zotero_attachment.ZoteroAttachmentClient(
                config_loader=lambda environ: (
                    "https://dav.example/zotero/",
                    "user",
                    "pass",
                    30.0,
                    "test",
                ),
                session=session,
                api=api,
            )
            result = client.import_pdf(
                api.USER_ID, api.PARENT_KEY, source, "PDF", "final.pdf"
            )

        self.assertTrue(result["already_present"])
        self.assertTrue(result["webdav_refreshed"])
        self.assertIsNone(api.payload)
        self.assertIn("<mtime>123</mtime>", session.puts[1][1].decode())


if __name__ == "__main__":
    unittest.main()
