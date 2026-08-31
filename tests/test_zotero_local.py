import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zotero_mcp import zotero_local


class PlatformDetectionTests(unittest.TestCase):
    def test_windows_default_api_uses_zotero_local_port(self):
        with mock.patch.object(zotero_local, "is_windows", return_value=True):
            self.assertEqual(
                zotero_local.default_api_base(), "http://127.0.0.1:23119/api"
            )

    def test_wsl_default_api_uses_gateway_proxy_port(self):
        with (
            mock.patch.object(zotero_local, "is_windows", return_value=False),
            mock.patch.object(
                zotero_local.zotero_http,
                "wsl_gateway_ip",
                return_value="172.30.1.1",
            ),
        ):
            self.assertEqual(
                zotero_local.local_api_candidates(),
                [
                    "http://127.0.0.1:23119/api",
                    "http://172.30.1.1:23119/api",
                    "http://172.30.1.1:23120/api",
                ],
            )

    def test_storage_env_override_wins(self):
        with mock.patch.dict(os.environ, {"ZOTERO_STORAGE": r"X:\Custom\storage"}):
            self.assertEqual(zotero_local.storage_root(), Path(r"X:\Custom\storage"))

    def test_windows_storage_prefers_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "D" / "Zotero" / "storage"
            second = Path(tmp) / "Users" / "funny" / "Zotero" / "storage"
            first.mkdir(parents=True)
            candidates = [first, second]

            with (
                mock.patch.object(zotero_local, "is_windows", return_value=True),
                mock.patch.object(
                    zotero_local, "windows_storage_candidates", return_value=candidates
                ),
                mock.patch.object(
                    zotero_local.zotero_runtime, "configured_path", return_value=None
                ),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(zotero_local.storage_root(), first)

    def test_wsl_storage_prefers_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "mnt" / "d" / "Zotero" / "storage"
            second = Path(tmp) / "mnt" / "c" / "Users" / "funny" / "Zotero" / "storage"
            first.mkdir(parents=True)
            candidates = [first, second]

            with (
                mock.patch.object(zotero_local, "is_windows", return_value=False),
                mock.patch.object(
                    zotero_local, "wsl_storage_candidates", return_value=candidates
                ),
                mock.patch.object(
                    zotero_local.zotero_runtime, "configured_path", return_value=None
                ),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(zotero_local.storage_root(), first)

    def test_default_text_out_dir_is_platform_temp_dir(self):
        expected = Path(tempfile.gettempdir()) / "zotero_texts"
        self.assertEqual(zotero_local.default_text_out_dir(), expected)

    def test_relative_text_output_uses_out_dir(self):
        self.assertEqual(
            zotero_local.resolve_text_output_path("paper.txt", "/tmp/zotero_texts", {}),
            Path("/tmp/zotero_texts/paper.txt"),
        )

    def test_nested_relative_text_output_uses_out_dir(self):
        self.assertEqual(
            zotero_local.resolve_text_output_path(
                "nested/paper.txt", "/tmp/zotero_texts", {}
            ),
            Path("/tmp/zotero_texts/nested/paper.txt"),
        )

    def test_relative_text_output_uses_default_temp_dir_without_out_dir(self):
        self.assertEqual(
            zotero_local.resolve_text_output_path("paper.txt", None, {}),
            zotero_local.default_text_out_dir() / "paper.txt",
        )

    def test_absolute_text_output_stays_absolute(self):
        self.assertEqual(
            zotero_local.resolve_text_output_path(
                "/tmp/custom/paper.txt", "/tmp/ignored", {}
            ),
            Path("/tmp/custom/paper.txt"),
        )

    def test_default_text_output_uses_default_filename(self):
        item = {"data": {"title": "A Long Paper Title For Testing"}}
        self.assertEqual(
            zotero_local.resolve_text_output_path(None, "/tmp/zotero_texts", item),
            Path("/tmp/zotero_texts/A_Long_Paper_Title_F.txt"),
        )

    def test_pdf_tool_env_override_wins(self):
        with mock.patch.dict(os.environ, {"PDFTOTEXT": r"C:\tools\pdftotext.exe"}):
            self.assertEqual(
                zotero_local.resolve_pdf_tool("pdftotext"),
                Path(r"C:\tools\pdftotext.exe"),
            )

    def test_active_env_prefix_prefers_running_interpreter_env(self):
        with (
            mock.patch.object(zotero_local, "is_windows", return_value=True),
            mock.patch.dict(os.environ, {"CONDA_PREFIX": r"D:\soft\miniforge3"}),
            mock.patch.object(
                zotero_local.sys, "prefix", r"D:\soft\miniforge3\envs\zotero-mcp"
            ),
        ):
            self.assertEqual(
                zotero_local.active_env_prefix(),
                Path(r"D:\soft\miniforge3\envs\zotero-mcp"),
            )

    def test_local_api_falls_back_and_caches_successful_candidate(self):
        response = mock.Mock()
        response.text = "[]"
        response.json.return_value = []
        with (
            mock.patch.object(
                zotero_local,
                "local_api_candidates",
                return_value=["http://first/api", "http://second/api"],
            ),
            mock.patch.object(
                zotero_local.zotero_http.requests,
                "request",
                side_effect=[
                    zotero_local.requests.ConnectionError("offline"),
                    response,
                ],
            ) as request,
            mock.patch.object(zotero_local, "_SELECTED_API_BASE", None),
        ):
            self.assertEqual(zotero_local.zotero_get("users/0/items"), [])
            self.assertEqual(zotero_local.api_base(), "http://second/api")
        self.assertEqual(request.call_count, 2)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.collections = [
            {
                "key": "ROOT",
                "meta": {"numItems": 1, "numCollections": 2},
                "data": {"key": "ROOT", "name": "Studies", "parentCollection": False},
            },
            {
                "key": "FOUNDATION",
                "meta": {"numItems": 2, "numCollections": 1},
                "data": {
                    "key": "FOUNDATION",
                    "name": "FoundationModels",
                    "parentCollection": "ROOT",
                },
            },
            {
                "key": "MODELS",
                "meta": {"numItems": 1, "numCollections": 0},
                "data": {
                    "key": "MODELS",
                    "name": "Models",
                    "parentCollection": "FOUNDATION",
                },
            },
            {
                "key": "OTHER",
                "meta": {"numItems": 1, "numCollections": 0},
                "data": {"key": "OTHER", "name": "Reviews", "parentCollection": False},
            },
        ]
        self.index = zotero_local.collection_index(self.collections)

    def test_collection_path_includes_all_parents(self):
        self.assertEqual(
            zotero_local.collection_path("MODELS", self.index),
            "Studies > FoundationModels > Models",
        )

    def test_descendant_collection_keys_are_recursive(self):
        self.assertEqual(
            zotero_local.descendant_collection_keys("ROOT", self.index),
            ["ROOT", "FOUNDATION", "MODELS"],
        )

    def test_item_can_belong_to_multiple_collections(self):
        item = {"data": {"key": "ITEM", "collections": ["FOUNDATION", "OTHER"]}}
        summaries = zotero_local.item_collection_summaries(item, self.index)
        self.assertEqual(
            [summary["path"] for summary in summaries],
            ["Reviews", "Studies > FoundationModels"],
        )

    def test_resolve_top_level_item_follows_parent_chain(self):
        attachment = {"data": {"key": "ATTACHMENT", "parentItem": "NOTE"}}
        note = {"data": {"key": "NOTE", "parentItem": "ITEM"}}
        item = {"data": {"key": "ITEM", "collections": ["FOUNDATION"]}}
        with mock.patch.object(
            zotero_local, "zotero_get", side_effect=[note, item]
        ) as zotero_get:
            self.assertEqual(zotero_local.resolve_top_level_item(attachment), item)
        self.assertEqual(
            [call.args[0] for call in zotero_get.call_args_list],
            ["users/0/items/NOTE", "users/0/items/ITEM"],
        )

    def test_recursive_collection_items_are_deduplicated(self):
        item_a = {"data": {"key": "A", "title": "Alpha"}}
        item_b = {"data": {"key": "B", "title": "Beta"}}
        child_note = {
            "data": {
                "key": "NOTE1",
                "itemType": "note",
                "parentItem": "A",
            }
        }
        pages = {
            "ROOT": [item_a, child_note],
            "FOUNDATION": [item_a, item_b],
            "MODELS": [item_b],
        }
        with mock.patch.object(
            zotero_local,
            "fetch_collection_items",
            side_effect=lambda key, limit: pages[key][:limit],
        ):
            items, memberships = zotero_local.collect_collection_items(
                "ROOT", self.index, recursive=True, limit=10
            )
        self.assertEqual([item["data"]["key"] for item in items], ["A", "B"])
        self.assertEqual(memberships["A"], ["ROOT", "FOUNDATION"])
        self.assertEqual(memberships["B"], ["FOUNDATION", "MODELS"])

    def test_list_collection_items_reuses_provided_collection_snapshot(self):
        with (
            mock.patch.object(zotero_local, "fetch_all_collections") as fetch,
            mock.patch.object(
                zotero_local,
                "collect_collection_items",
                return_value=([], {}),
            ),
        ):
            result = zotero_local.list_collection_items(
                "ROOT",
                collections=self.collections,
            )
        self.assertEqual(result["collection"]["key"], "ROOT")
        self.assertFalse(fetch.called)

    def test_item_summary_includes_extra_metadata(self):
        item = {
            "data": {
                "key": "ITEM",
                "itemType": "journalArticle",
                "title": "Paper",
                "abstractNote": "",
                "extra": "TLDR: Short fallback summary.",
            }
        }
        with mock.patch.object(
            zotero_local, "attachment_key_from_links", return_value=None
        ):
            summary = zotero_local.item_summary(item)
        self.assertEqual(summary["extra"], "TLDR: Short fallback summary.")


class EnglishPdfSelectionTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "data": {"key": "ITEM1"},
            "links": {"attachment": {"href": "http://localhost/items/ORIGINAL"}},
        }

    def test_prefers_english_original_over_chinese_translation(self):
        children = [
            {
                "data": {
                    "key": "ORIGINAL",
                    "title": "PDF",
                    "filename": "paper.pdf",
                    "contentType": "application/pdf",
                }
            },
            {
                "data": {
                    "key": "TRANSLATED",
                    "title": "CN",
                    "filename": "paper_full_translation.pdf",
                    "contentType": "application/pdf",
                }
            },
        ]
        paths = {
            "ORIGINAL": [Path("/tmp/original.pdf")],
            "TRANSLATED": [Path("/tmp/translated.pdf")],
        }
        with (
            mock.patch.object(zotero_local, "zotero_get", return_value=children),
            mock.patch.object(
                zotero_local,
                "find_pdf_for_attachment",
                side_effect=lambda key: paths[key],
            ),
            mock.patch.object(
                zotero_local, "pdf_text_language_stats", return_value=(0.01, 5000)
            ),
        ):
            attachment = zotero_local.english_pdf_attachment_for_item(self.item)
            self.assertEqual(attachment["key"], "ORIGINAL")
            self.assertEqual(attachment["path"], Path("/tmp/original.pdf"))
            self.assertEqual(
                zotero_local.english_pdf_for_item(self.item),
                Path("/tmp/original.pdf"),
            )

    def test_prefers_primary_article_over_english_supplement(self):
        children = [
            {
                "data": {
                    "key": "ORIGINAL",
                    "title": "Full Text PDF",
                    "filename": "paper.pdf",
                    "contentType": "application/pdf",
                }
            },
            {
                "data": {
                    "key": "SUPPLEMENT",
                    "title": "SI",
                    "filename": "supplement.pdf",
                    "contentType": "application/pdf",
                }
            },
        ]
        paths = {
            "ORIGINAL": [Path("/tmp/original.pdf")],
            "SUPPLEMENT": [Path("/tmp/supplement.pdf")],
        }
        with (
            mock.patch.object(zotero_local, "zotero_get", return_value=children),
            mock.patch.object(
                zotero_local,
                "find_pdf_for_attachment",
                side_effect=lambda key: paths[key],
            ),
            mock.patch.object(
                zotero_local,
                "pdf_text_language_stats",
                side_effect=lambda path: (
                    (0.01, 5000) if path.name == "original.pdf" else (0.0, 5000)
                ),
            ),
        ):
            self.assertEqual(
                zotero_local.english_pdf_for_item(self.item),
                Path("/tmp/original.pdf"),
            )

    def test_rejects_chinese_only_attachment(self):
        children = [
            {
                "data": {
                    "key": "TRANSLATED",
                    "title": "PDF",
                    "filename": "paper.pdf",
                    "contentType": "application/pdf",
                }
            }
        ]
        with (
            mock.patch.object(zotero_local, "zotero_get", return_value=children),
            mock.patch.object(
                zotero_local,
                "find_pdf_for_attachment",
                return_value=[Path("/tmp/translated.pdf")],
            ),
            mock.patch.object(
                zotero_local, "pdf_text_language_stats", return_value=(0.60, 5000)
            ),
            self.assertRaisesRegex(SystemExit, "No confidently English PDF"),
        ):
            zotero_local.english_pdf_for_item(self.item)

    def test_chinese_in_paper_title_is_not_a_translation_marker(self):
        self.assertFalse(
            zotero_local.has_translation_marker(
                "PDF", "2026_Science_Chinese Immune Multi-Omics Atlas.pdf"
            )
        )


class ReadOnlyDetailTests(unittest.TestCase):
    def test_item_annotations_follow_parent_attachment_annotation_chain(self):
        parent = {
            "key": "ITEM0001",
            "data": {"key": "ITEM0001", "itemType": "journalArticle"},
        }
        attachment = {
            "key": "PDF00001",
            "data": {
                "key": "PDF00001",
                "itemType": "attachment",
                "title": "Main PDF",
            },
        }
        annotation = {
            "key": "ANN00001",
            "data": {
                "key": "ANN00001",
                "itemType": "annotation",
                "annotationType": "highlight",
                "annotationText": "Important result",
                "annotationPageLabel": "5",
                "annotationPosition": '{"pageIndex":4}',
                "tags": [{"tag": "result"}],
            },
        }

        def paginate(path, _params=None, max_items=None):
            if path.endswith("ITEM0001/children"):
                return [attachment]
            if path.endswith("PDF00001/children"):
                return [annotation][:max_items]
            return []

        with (
            mock.patch.object(zotero_local, "zotero_get", return_value=parent),
            mock.patch.object(zotero_local, "fetch_paginated", side_effect=paginate),
        ):
            records = zotero_local.item_annotations("ITEM0001", limit=10)

        self.assertEqual(records[0]["annotation_key"], "ANN00001")
        self.assertEqual(records[0]["attachment_key"], "PDF00001")
        self.assertEqual(records[0]["page_index"], 4)
        self.assertEqual(records[0]["tags"], ["result"])

    def test_item_notes_include_html_and_plain_text(self):
        parent = {
            "key": "ITEM0001",
            "data": {"key": "ITEM0001", "itemType": "journalArticle"},
        }
        note = {
            "key": "NOTE0001",
            "data": {
                "key": "NOTE0001",
                "itemType": "note",
                "note": "<p>First line<br>Second &amp; third</p>",
                "tags": [],
            },
        }
        with (
            mock.patch.object(zotero_local, "zotero_get", return_value=parent),
            mock.patch.object(zotero_local, "fetch_paginated", return_value=[note]),
        ):
            records = zotero_local.item_notes("ITEM0001")

        self.assertEqual(records[0]["note_key"], "NOTE0001")
        self.assertIn("<p>", records[0]["note_html"])
        self.assertEqual(records[0]["note_text"], "First line\nSecond & third")


if __name__ == "__main__":
    unittest.main()
