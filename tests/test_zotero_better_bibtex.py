import unittest
from unittest import mock

import requests

from zotero_mcp import zotero_better_bibtex


class BetterBibTeXTests(unittest.TestCase):
    def response(self, payload):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_citation_key_uses_named_item_keys(self):
        with (
            mock.patch.object(
                zotero_better_bibtex.zotero_local,
                "api_base",
                return_value="http://172.29.112.1:23120/api",
            ),
            mock.patch.object(
                zotero_better_bibtex.zotero_http,
                "post",
                return_value=self.response(
                    {"jsonrpc": "2.0", "result": {"ITEM0001": "Smith2024"}, "id": 1}
                ),
            ) as post,
        ):
            result = zotero_better_bibtex.citation_key("ITEM0001")

        self.assertEqual(result, "Smith2024")
        self.assertEqual(
            post.call_args.args[0],
            "http://172.29.112.1:23120/better-bibtex/json-rpc",
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["params"], {"item_keys": ["ITEM0001"]}
        )

    def test_export_bibtex_returns_citation_key_and_text(self):
        responses = [
            self.response(
                {"jsonrpc": "2.0", "result": {"ITEM0001": "Smith2024"}, "id": 1}
            ),
            self.response(
                {"jsonrpc": "2.0", "result": "@article{Smith2024,\n}\n", "id": 1}
            ),
        ]
        with mock.patch.object(
            zotero_better_bibtex.zotero_http,
            "post",
            side_effect=responses,
        ):
            result = zotero_better_bibtex.export_bibtex("ITEM0001")

        self.assertEqual(result.citation_key, "Smith2024")
        self.assertIn("@article{Smith2024", result.bibtex)

    def test_connection_error_is_actionable(self):
        with (
            mock.patch.object(
                zotero_better_bibtex.zotero_http,
                "post",
                side_effect=requests.ConnectionError("offline"),
            ),
            self.assertRaisesRegex(
                zotero_better_bibtex.BetterBibTeXError,
                "Ensure Zotero is running",
            ),
        ):
            zotero_better_bibtex.citation_key("ITEM0001")

    def test_citation_key_fallback_parser_reads_extra(self):
        item = {"data": {"extra": "DOI: 10.1/example\nCitation Key: Smith2024"}}
        self.assertEqual(zotero_better_bibtex.citation_key_from_item(item), "Smith2024")


if __name__ == "__main__":
    unittest.main()
