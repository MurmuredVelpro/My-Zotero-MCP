import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import anyio

os.environ["ZOTERO_MCP_DISABLE_PRIVATE"] = "1"

from zotero_mcp import zotero_mcp_server

READ_ONLY = (True, False, True, True)
READ_ONLY_CLOSED = (True, False, True, False)
WRITE = (False, False, True, True)
TOOL_CONTRACTS = {
    "zotero_ping": ((), (), READ_ONLY),
    "zotero_search": (("query", "limit", "item_type"), ("query",), READ_ONLY),
    "zotero_match": (
        ("query", "field", "limit", "scan_limit", "threshold", "best"),
        ("query",),
        READ_ONLY,
    ),
    "zotero_item": (("key", "format"), ("key",), READ_ONLY),
    "zotero_children": (("key",), ("key",), READ_ONLY),
    "zotero_get_annotations": (("key", "limit"), ("key",), READ_ONLY),
    "zotero_get_notes": (("key", "limit"), ("key",), READ_ONLY),
    "zotero_get_citation_key": (("key",), ("key",), READ_ONLY),
    "zotero_collections": ((), (), READ_ONLY),
    "zotero_resolve_collection": (
        ("key", "name", "path"),
        (),
        READ_ONLY_CLOSED,
    ),
    "zotero_item_collections": (("key",), ("key",), READ_ONLY),
    "zotero_collection_items": (
        ("key", "collection", "recursive", "limit"),
        (),
        READ_ONLY,
    ),
    "zotero_web_api_status": ((), (), READ_ONLY),
    "zotero_plan_paper_import": (("items",), ("items",), READ_ONLY_CLOSED),
    "zotero_apply_paper_import": (
        ("items", "confirm"),
        ("items", "confirm"),
        WRITE,
    ),
    "zotero_plan_collection_reconcile": (
        ("items", "allow_no_collections"),
        ("items",),
        READ_ONLY_CLOSED,
    ),
    "zotero_apply_collection_reconcile": (
        ("items", "confirm", "allow_no_collections"),
        ("items", "confirm"),
        (False, True, True, True),
    ),
    "zotero_plan_pdf_attachment_delete": (
        ("collection_key", "collection", "recursive", "limit", "offset", "page_size"),
        (),
        READ_ONLY_CLOSED,
    ),
    "zotero_apply_pdf_attachment_delete": (
        (
            "collection_key",
            "items",
            "backup_dir",
            "confirm",
            "recursive",
            "allow_shared_parents",
            "allow_annotations",
        ),
        ("collection_key", "items", "backup_dir", "confirm"),
        (False, True, False, True),
    ),
    "zotero_plan_manual_translation_rename": (
        ("item_keys",),
        ("item_keys",),
        READ_ONLY,
    ),
    "zotero_apply_manual_translation_rename": (
        ("items", "confirm"),
        ("items", "confirm"),
        (False, True, True, True),
    ),
    "zotero_extract_text": (
        ("key", "out_dir", "output", "attachment_priority"),
        ("key",),
        WRITE,
    ),
    "zotero_mineru_submit": (
        (
            "key",
            "confirm",
            "language",
            "enable_formula",
            "enable_table",
            "is_ocr",
            "page_ranges",
        ),
        ("key", "confirm"),
        (False, False, False, True),
    ),
    "zotero_mineru_result": (("batch_id", "out_dir"), ("batch_id",), WRITE),
    "zotero_render_pages": (
        ("key", "pages", "out_dir", "dpi", "format"),
        ("key", "pages", "out_dir"),
        WRITE,
    ),
    "zotero_find_figure_pages": (
        ("key", "figure", "limit", "context"),
        ("key", "figure"),
        READ_ONLY,
    ),
}


class CollectionToolTests(unittest.TestCase):
    def test_public_tool_contracts_are_locked(self):
        definitions = zotero_mcp_server.tool_definitions("all")
        self.assertEqual([tool["name"] for tool in definitions], list(TOOL_CONTRACTS))
        for tool in definitions:
            properties, required, annotations = TOOL_CONTRACTS[tool["name"]]
            self.assertEqual(
                tuple(tool["inputSchema"].get("properties", {})), properties
            )
            self.assertEqual(tuple(tool["inputSchema"].get("required", ())), required)
            actual_annotations = tool["annotations"]
            self.assertEqual(
                (
                    actual_annotations["readOnlyHint"],
                    actual_annotations["destructiveHint"],
                    actual_annotations["idempotentHint"],
                    actual_annotations["openWorldHint"],
                ),
                annotations,
            )
            self.assertEqual(tool["outputSchema"], zotero_mcp_server.TOOL_OUTPUT_SCHEMA)

    def test_plan_schemas_accept_references_but_apply_schemas_require_keys(self):
        tools = {
            tool["name"]: tool for tool in zotero_mcp_server.tool_definitions("all")
        }
        paper_plan = tools["zotero_plan_paper_import"]["inputSchema"]["properties"][
            "items"
        ]["items"]
        paper_apply = tools["zotero_apply_paper_import"]["inputSchema"]["properties"][
            "items"
        ]["items"]
        self.assertIn("collections", paper_plan["properties"])
        self.assertNotIn("collections", paper_apply["properties"])
        self.assertIn("collection_keys", paper_apply["required"])

        reconcile_plan = tools["zotero_plan_collection_reconcile"]["inputSchema"][
            "properties"
        ]["items"]["items"]
        reconcile_apply = tools["zotero_apply_collection_reconcile"]["inputSchema"][
            "properties"
        ]["items"]["items"]
        self.assertIn("add_collections", reconcile_plan["properties"])
        self.assertNotIn("add_collections", reconcile_apply["properties"])

        pdf_plan = tools["zotero_plan_pdf_attachment_delete"]["inputSchema"]
        pdf_apply = tools["zotero_apply_pdf_attachment_delete"]["inputSchema"]
        self.assertIn("collection", pdf_plan["properties"])
        self.assertNotIn("collection", pdf_apply["properties"])
        self.assertIn("collection_key", pdf_apply["required"])

    def test_mcp_read_tools_do_not_capture_cli_stdout(self):
        self.assertFalse(hasattr(zotero_mcp_server, "capture_output"))
        self.assertFalse(hasattr(zotero_mcp_server, "Args"))
        self.assertFalse(hasattr(zotero_mcp_server, "mineru_workflow"))

    def test_printed_codex_config_uses_current_runtime(self):
        config = zotero_mcp_server.codex_config_toml(
            ("literature", "review", "maintenance")
        )
        self.assertIn("[mcp_servers.zotero]", config)
        self.assertIn("[mcp_servers.qmd]", config)
        self.assertIn("[mcp_servers.sciverse]", config)
        self.assertIn(zotero_mcp_server.sys.executable.replace("\\", "\\\\"), config)
        self.assertIn("literature,review,maintenance", config)

    def test_search_summarizes_each_item_once(self):
        items = [{"data": {"key": "ITEM0001"}}, {"data": {"key": "ITEM0002"}}]
        summaries = [{"key": "ITEM0001"}, {"key": "ITEM0002"}]
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "search_items",
                return_value=items,
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "item_summary",
                side_effect=summaries,
            ) as summarize,
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "format_item_summaries",
                return_value="items",
            ) as format_summaries,
        ):
            result = zotero_mcp_server.tool_search({"query": "aging"})

        self.assertEqual(summarize.call_count, 2)
        format_summaries.assert_called_once_with(summaries, "No matching Zotero items.")
        self.assertEqual(result["structuredContent"]["data"]["items"], summaries)

    def test_readme_tool_list_matches_public_contract(self):
        readme = (
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("README.md")
            .read_text(encoding="utf-8")
        )
        section = readme.split("可用 MCP 工具：", 1)[1].lstrip().split("\n\n", 1)[0]
        documented = re.findall(r"^- `([^`]+)`$", section, flags=re.MULTILINE)
        self.assertEqual(documented, list(TOOL_CONTRACTS))

    def test_collection_tools_are_registered(self):
        names = {tool["name"] for tool in zotero_mcp_server.tool_definitions("all")}
        self.assertTrue(
            {
                "zotero_collections",
                "zotero_resolve_collection",
                "zotero_item_collections",
                "zotero_collection_items",
            }.issubset(names)
        )

    def test_mineru_tools_are_registered(self):
        names = {tool["name"] for tool in zotero_mcp_server.tool_definitions("all")}
        self.assertTrue(
            {
                "zotero_mineru_submit",
                "zotero_mineru_result",
            }.issubset(names)
        )
        self.assertNotIn("zotero_mineru_local_result", names)

    def test_all_toolset_contains_every_public_tool(self):
        tools = {
            tool["name"]: tool for tool in zotero_mcp_server.tool_definitions("all")
        }
        self.assertEqual(len(tools), 26)
        self.assertTrue(
            {
                "zotero_get_annotations",
                "zotero_get_notes",
                "zotero_get_citation_key",
                "zotero_apply_pdf_attachment_delete",
                "zotero_plan_manual_translation_rename",
                "zotero_apply_manual_translation_rename",
            }.issubset(tools)
        )

    def test_default_and_union_toolsets_preserve_registry_order(self):
        default_names = [tool["name"] for tool in zotero_mcp_server.tool_definitions()]
        self.assertEqual(
            default_names,
            list(zotero_mcp_server.TOOLSETS["literature"]),
        )

        union_names = zotero_mcp_server.selected_tool_names("literature,review")
        expected = [
            name
            for name in zotero_mcp_server.TOOLS
            if name
            in set(zotero_mcp_server.TOOLSETS["literature"])
            | set(zotero_mcp_server.TOOLSETS["review"])
        ]
        self.assertEqual(list(union_names), expected)

    def test_toolsets_cover_every_tool_and_reject_unknown_names(self):
        assigned = {
            name for names in zotero_mcp_server.TOOLSETS.values() for name in names
        }
        self.assertEqual(assigned, set(zotero_mcp_server.TOOLS))
        with self.assertRaisesRegex(ValueError, "unknown toolsets"):
            zotero_mcp_server.parse_toolsets("literature,unknown")

    def test_hidden_tool_cannot_be_called_by_name(self):
        original = zotero_mcp_server.ACTIVE_TOOLSETS

        async def invoke():
            return await zotero_mcp_server.call_tool(
                "zotero_get_annotations",
                {"key": "ITEM0001"},
            )

        try:
            zotero_mcp_server.configure_toolsets("literature")
            result = anyio.run(invoke)
        finally:
            zotero_mcp_server.configure_toolsets(original)

        self.assertTrue(result.isError)
        self.assertEqual(result.content[0].text, "Unknown tool: zotero_get_annotations")

    def test_all_tools_have_output_schema_and_annotations(self):
        for tool in zotero_mcp_server.tool_definitions("all"):
            self.assertEqual(tool["outputSchema"], zotero_mcp_server.TOOL_OUTPUT_SCHEMA)
            self.assertEqual(
                set(tool["annotations"]),
                {
                    "readOnlyHint",
                    "destructiveHint",
                    "idempotentHint",
                    "openWorldHint",
                },
            )

    def test_guarded_write_tools_are_registered_with_annotations(self):
        tools = {
            tool["name"]: tool for tool in zotero_mcp_server.tool_definitions("all")
        }
        self.assertTrue(
            {
                "zotero_web_api_status",
                "zotero_plan_paper_import",
                "zotero_apply_paper_import",
                "zotero_plan_collection_reconcile",
                "zotero_apply_collection_reconcile",
                "zotero_plan_pdf_attachment_delete",
                "zotero_apply_pdf_attachment_delete",
                "zotero_plan_manual_translation_rename",
                "zotero_apply_manual_translation_rename",
            }.issubset(tools)
        )
        self.assertTrue(
            tools["zotero_plan_paper_import"]["annotations"]["readOnlyHint"]
        )
        self.assertFalse(
            tools["zotero_apply_paper_import"]["annotations"]["destructiveHint"]
        )
        self.assertTrue(
            tools["zotero_apply_paper_import"]["annotations"]["idempotentHint"]
        )
        self.assertTrue(
            tools["zotero_plan_collection_reconcile"]["annotations"]["readOnlyHint"]
        )
        self.assertTrue(
            tools["zotero_apply_collection_reconcile"]["annotations"]["destructiveHint"]
        )
        self.assertTrue(
            tools["zotero_apply_collection_reconcile"]["annotations"]["idempotentHint"]
        )
        self.assertTrue(
            tools["zotero_plan_pdf_attachment_delete"]["annotations"]["readOnlyHint"]
        )
        self.assertTrue(
            tools["zotero_apply_pdf_attachment_delete"]["annotations"][
                "destructiveHint"
            ]
        )
        self.assertFalse(
            tools["zotero_apply_pdf_attachment_delete"]["annotations"]["idempotentHint"]
        )
        self.assertTrue(
            tools["zotero_plan_manual_translation_rename"]["annotations"][
                "readOnlyHint"
            ]
        )
        self.assertTrue(
            tools["zotero_apply_manual_translation_rename"]["annotations"][
                "destructiveHint"
            ]
        )
        self.assertTrue(tools["zotero_web_api_status"]["annotations"]["openWorldHint"])

    def test_manual_translation_rename_tools_forward_guarded_requests(self):
        planned = {"rename": 1, "results": []}
        applied = {"renamed": 1, "results": []}
        with mock.patch.object(
            zotero_mcp_server.zotero_translate,
            "plan_manual_translation_renames",
            return_value=planned,
        ) as plan:
            result = zotero_mcp_server.tool_plan_manual_translation_rename(
                {"item_keys": ["ABCD1234"]}
            )
        self.assertFalse(result["isError"])
        plan.assert_called_once_with(["ABCD1234"])

        items = [
            {
                "parent_item_key": "ABCD1234",
                "source_attachment_key": "EFGH5678",
                "translation_attachment_key": "CNAT1234",
                "new_title": "CN",
                "new_filename": "Paper的全文翻译.pdf",
            }
        ]
        with mock.patch.object(
            zotero_mcp_server.zotero_translate,
            "apply_manual_translation_renames",
            return_value=applied,
        ) as apply:
            result = zotero_mcp_server.tool_apply_manual_translation_rename(
                {"items": items, "confirm": True}
            )
        self.assertFalse(result["isError"])
        apply.assert_called_once_with(items, True)

        required = set(zotero_mcp_server.TRANSLATION_RENAME_ITEM_SCHEMA["required"])
        self.assertEqual(
            required,
            {
                "parent_item_key",
                "source_attachment_key",
                "translation_attachment_key",
                "new_title",
                "new_filename",
            },
        )

    def test_web_api_status_tool_checks_web_api(self):
        status = {
            "ok": True,
            "transport": "zotero_web_api",
            "user_id": 12345678,
            "personal_library_write": True,
        }
        with mock.patch.object(
            zotero_mcp_server.zotero_write,
            "web_api_status",
            return_value=status,
        ) as check:
            result = zotero_mcp_server.tool_web_api_status({})
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), status)
        check.assert_called_once_with()

    def test_mineru_submit_uses_zotero_pdf(self):
        item = {"data": {"key": "ITEM1"}}
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local, "zotero_get", return_value=item
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "english_pdf_attachment_for_item",
                return_value={"key": "PDF1", "path": Path("/tmp/paper.pdf")},
            ),
            mock.patch.object(
                zotero_mcp_server.mineru_client,
                "submit_file",
                return_value={"batch_id": "B1"},
            ) as submit,
            mock.patch.object(
                zotero_mcp_server.mineru_client,
                "find_local_result",
                return_value=None,
            ),
        ):
            result = zotero_mcp_server.tool_mineru_submit(
                {"key": "ITEM1", "confirm": True}
            )
        self.assertFalse(result["isError"])
        self.assertEqual(submit.call_args.kwargs["data_id"], "ITEM1")
        self.assertEqual(submit.call_args.kwargs["model_version"], "vlm")
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("recoverable collection batches", payload["next_action"])

    def test_mineru_pending_result_requests_another_poll(self):
        batch = {"extract_result": [{"data_id": "ITEM1", "state": "running"}]}
        with mock.patch.object(
            zotero_mcp_server.mineru_client, "get_batch", return_value=batch
        ):
            result = zotero_mcp_server.tool_mineru_result({"batch_id": "B1"})
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["state"], "running")
        self.assertIn("Poll this batch again", payload["next_action"])

    def test_mineru_submit_reuses_existing_result(self):
        item = {"data": {"key": "ITEM1"}}
        existing = {
            "output_dir": "/tmp/ITEM1",
            "full_md": "/tmp/ITEM1/full.md",
        }
        workflow = mock.Mock()
        workflow.tracked_result_status.return_value = "current"
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local, "zotero_get", return_value=item
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "english_pdf_attachment_for_item",
                return_value={"key": "PDF1", "path": Path("/tmp/paper.pdf")},
            ),
            mock.patch.object(
                zotero_mcp_server.mineru_client,
                "find_local_result",
                return_value=existing,
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_extract,
                "load_mineru_workflow",
                return_value=workflow,
            ),
            mock.patch.object(
                zotero_mcp_server.mineru_client,
                "artifact_summary",
                return_value={"full_md": "/tmp/ITEM1/full.md"},
            ),
            mock.patch.object(zotero_mcp_server.mineru_client, "submit_file") as submit,
        ):
            result = zotero_mcp_server.tool_mineru_submit(
                {"key": "ITEM1", "confirm": True}
            )
        self.assertFalse(result["isError"])
        self.assertFalse(submit.called)

    def test_mineru_submit_refuses_stale_tracked_result(self):
        item = {"data": {"key": "ITEM1"}}
        workflow = mock.Mock()
        workflow.tracked_result_status.return_value = "stale"
        workflow.mineru_records_by_key.return_value = {
            "ITEM1": {"parsed_attachment_key": "OLDPDF1"}
        }
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local, "zotero_get", return_value=item
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "english_pdf_attachment_for_item",
                return_value={"key": "NEWPDF1", "path": Path("/tmp/new.pdf")},
            ),
            mock.patch.object(
                zotero_mcp_server.mineru_client,
                "find_local_result",
                return_value={"full_md": "/tmp/ITEM1/full.md"},
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_extract,
                "load_mineru_workflow",
                return_value=workflow,
            ),
            mock.patch.object(zotero_mcp_server.mineru_client, "submit_file") as submit,
        ):
            result = zotero_mcp_server.tool_mineru_submit(
                {"key": "ITEM1", "confirm": True}
            )
        self.assertTrue(result["isError"])
        self.assertIn("stale", result["content"][0]["text"].lower())
        self.assertFalse(submit.called)

    def test_mineru_submit_requires_explicit_confirmation(self):
        with (
            mock.patch.object(zotero_mcp_server.zotero_local, "zotero_get") as get,
            mock.patch.object(zotero_mcp_server.mineru_client, "submit_file") as submit,
        ):
            result = zotero_mcp_server.tool_mineru_submit(
                {"key": "ITEM1", "confirm": False}
            )
        self.assertTrue(result["isError"])
        self.assertIn("confirm=true", result["content"][0]["text"])
        get.assert_not_called()
        submit.assert_not_called()

    def test_item_collections_requires_key(self):
        result = zotero_mcp_server.tool_item_collections({})
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "Missing required argument: key")

    def test_item_json_format_returns_complete_record(self):
        item = {"key": "ITEM0001", "version": 7, "data": {"title": "Paper"}}
        with mock.patch.object(
            zotero_mcp_server.zotero_local, "zotero_get", return_value=item
        ):
            result = zotero_mcp_server.tool_item({"key": "ITEM0001", "format": "json"})
        self.assertEqual(result["structuredContent"]["data"], item)

    def test_item_bibtex_format_uses_better_bibtex(self):
        exported = zotero_mcp_server.zotero_better_bibtex.BibTeXExport(
            item_key="ITEM0001",
            citation_key="Smith2024",
            bibtex="@article{Smith2024,\n}\n",
        )
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "zotero_get",
                return_value={"key": "ITEM0001", "data": {}},
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_better_bibtex,
                "export_bibtex",
                return_value=exported,
            ),
        ):
            result = zotero_mcp_server.tool_item(
                {"key": "ITEM0001", "format": "bibtex"}
            )
        self.assertEqual(
            result["structuredContent"]["data"]["citation_key"], "Smith2024"
        )

    def test_citation_key_falls_back_to_stored_better_bibtex_key(self):
        item = {"data": {"extra": "Citation Key: Smith2024"}}
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local, "zotero_get", return_value=item
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_better_bibtex,
                "citation_key",
                side_effect=zotero_mcp_server.zotero_better_bibtex.BetterBibTeXError(
                    "offline"
                ),
            ),
        ):
            result = zotero_mcp_server.tool_citation_key({"key": "ITEM0001"})
        data = result["structuredContent"]["data"]
        self.assertEqual(data["citation_key"], "Smith2024")
        self.assertEqual(data["source"], "zotero_item")

    def test_extract_text_reports_selected_source(self):
        document = zotero_mcp_server.zotero_extract.ExtractedDoc(
            text="full text",
            item_key="ITEM0001",
            attachment_key="PDF00001",
            source="mineru",
            source_path=Path("/tmp/full.md"),
            output_path=Path("/tmp/paper.txt"),
        )
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "zotero_get",
                return_value={"key": "ITEM0001", "data": {}},
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_extract,
                "extract_document",
                return_value=document,
            ) as extract,
        ):
            result = zotero_mcp_server.tool_extract_text(
                {"key": "ITEM0001", "attachment_priority": "mineru_only"}
            )
        self.assertEqual(result["structuredContent"]["data"]["source"], "mineru")
        extract.assert_called_once()

    def test_unexpected_tool_error_does_not_return_traceback(self):
        original = zotero_mcp_server.TOOLS["zotero_ping"]["handler"]
        zotero_mcp_server.TOOLS["zotero_ping"]["handler"] = mock.Mock(
            side_effect=RuntimeError("internal detail")
        )

        async def invoke():
            return await zotero_mcp_server.call_tool("zotero_ping", {})

        try:
            result = anyio.run(invoke)
        finally:
            zotero_mcp_server.TOOLS["zotero_ping"]["handler"] = original

        self.assertTrue(result.isError)
        self.assertNotIn("Traceback", result.content[0].text)
        self.assertNotIn("internal detail", result.content[0].text)

    def test_collection_items_forwards_options(self):
        payload = {
            "collection": {"key": "FOUNDATION", "path": "Studies > FoundationModels"},
            "recursive": True,
            "count": 0,
            "items": [],
        }
        with mock.patch.object(
            zotero_mcp_server.zotero_local,
            "list_collection_items",
            return_value=payload,
        ) as query:
            result = zotero_mcp_server.tool_collection_items(
                {"key": "FOUNDATION", "recursive": True, "limit": 25}
            )
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["data"], payload)
        query.assert_called_once_with("FOUNDATION", recursive=True, limit=25)

    def test_resolve_collection_tool_returns_canonical_json(self):
        resolved = {
            "key": "GLIOMA01",
            "name": "Glioma",
            "path": "Projects > Glioma",
            "parent_key": "ROOT0001",
            "match_kind": "path",
        }
        with mock.patch.object(
            zotero_mcp_server.zotero_collections,
            "resolve_collection",
            return_value=resolved,
        ) as resolve:
            result = zotero_mcp_server.tool_resolve_collection(
                {"path": "Projects > Glioma"}
            )
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["data"], resolved)
        resolve.assert_called_once_with({"path": "Projects > Glioma"})

    def test_collection_items_resolves_reference_before_read(self):
        payload = {
            "collection": {
                "key": "GLIOMA01",
                "path": "Projects > Glioma",
            },
            "recursive": False,
            "collection_keys": ["GLIOMA01"],
            "count": 0,
            "items": [],
        }
        rows = [
            {
                "key": "GLIOMA01",
                "data": {
                    "key": "GLIOMA01",
                    "name": "Glioma",
                    "parentCollection": False,
                },
            }
        ]
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "fetch_all_collections",
                return_value=rows,
            ) as fetch,
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "list_collection_items",
                return_value=payload,
            ) as query,
        ):
            result = zotero_mcp_server.tool_collection_items(
                {"collection": {"name": "Glioma"}, "limit": 25}
            )
        self.assertFalse(result["isError"])
        fetch.assert_called_once_with()
        query.assert_called_once_with(
            "GLIOMA01",
            recursive=False,
            limit=25,
            collections=rows,
        )

    def test_plan_paper_import_returns_json(self):
        payload = {"total": 1, "summary": {"create": 1}, "results": []}
        with mock.patch.object(
            zotero_mcp_server.zotero_write,
            "plan_paper_import",
            return_value=payload,
        ) as plan:
            result = zotero_mcp_server.tool_plan_paper_import(
                {"items": [{"title": "A"}]}
            )
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        plan.assert_called_once_with([{"title": "A"}])

    def test_plan_paper_import_resolves_references_to_exact_keys(self):
        payload = {"total": 1, "summary": {"create": 1}, "results": []}
        records = [
            {
                "title": "A",
                "collections": [{"path": "Projects > Glioma"}],
            }
        ]
        rows = [
            {
                "key": "ROOT0001",
                "data": {
                    "key": "ROOT0001",
                    "name": "Projects",
                    "parentCollection": False,
                },
            },
            {
                "key": "GLIOMA01",
                "data": {
                    "key": "GLIOMA01",
                    "name": "Glioma",
                    "parentCollection": "ROOT0001",
                },
            },
        ]
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "fetch_all_collections",
                return_value=rows,
            ) as fetch,
            mock.patch.object(
                zotero_mcp_server.zotero_write,
                "plan_paper_import",
                return_value=payload,
            ) as plan,
        ):
            result = zotero_mcp_server.tool_plan_paper_import({"items": records})
        self.assertFalse(result["isError"])
        fetch.assert_called_once_with()
        plan.assert_called_once_with(
            [{"title": "A", "collection_keys": ["GLIOMA01"]}],
            collection_keys={"ROOT0001", "GLIOMA01"},
        )

    def test_plan_paper_import_rejects_mixed_reference_styles(self):
        records = [
            {
                "title": "A",
                "collection_keys": ["GLIOMA01"],
                "collections": [{"name": "Glioma"}],
            }
        ]
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "fetch_all_collections",
                return_value=[],
            ) as fetch,
            mock.patch.object(
                zotero_mcp_server.zotero_write,
                "plan_paper_import",
            ) as plan,
        ):
            result = zotero_mcp_server.tool_plan_paper_import({"items": records})
        self.assertTrue(result["isError"])
        self.assertIn("cannot mix", result["content"][0]["text"])
        self.assertFalse(fetch.called)
        self.assertFalse(plan.called)

    def test_apply_paper_import_requires_explicit_true_confirmation(self):
        with mock.patch.object(
            zotero_mcp_server.zotero_write,
            "execute_paper_import",
            side_effect=zotero_mcp_server.zotero_write.ZoteroWriteError(
                "confirm=true is required for Zotero writes"
            ),
        ) as execute:
            result = zotero_mcp_server.tool_apply_paper_import(
                {"items": [{"title": "A"}], "confirm": False}
            )
        self.assertTrue(result["isError"])
        self.assertIn("confirm=true", result["content"][0]["text"])
        execute.assert_called_once_with([{"title": "A"}], False)

    def test_plan_collection_reconcile_returns_json(self):
        payload = {"total": 1, "summary": {"reconcile": 1}, "results": []}
        records = [
            {
                "item_key": "SUZCN22Y",
                "add_collection_keys": [],
                "remove_collection_keys": ["XM2WM4D4"],
            }
        ]
        with mock.patch.object(
            zotero_mcp_server.zotero_write,
            "plan_collection_reconcile",
            return_value=payload,
        ) as plan:
            result = zotero_mcp_server.tool_plan_collection_reconcile(
                {"items": records}
            )
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        plan.assert_called_once_with(records, allow_no_collections=False)

    def test_plan_collection_reconcile_resolves_reference_fields(self):
        payload = {"total": 1, "summary": {"reconcile": 1}, "results": []}
        records = [
            {
                "item_key": "SUZCN22Y",
                "add_collections": [{"name": "Glioma"}],
                "remove_collections": [{"path": "Projects > Aging"}],
            }
        ]
        rows = [
            {
                "key": "ROOT0001",
                "data": {
                    "key": "ROOT0001",
                    "name": "Projects",
                    "parentCollection": False,
                },
            },
            {
                "key": "GLIOMA01",
                "data": {
                    "key": "GLIOMA01",
                    "name": "Glioma",
                    "parentCollection": "ROOT0001",
                },
            },
            {
                "key": "AGING001",
                "data": {
                    "key": "AGING001",
                    "name": "Aging",
                    "parentCollection": "ROOT0001",
                },
            },
        ]

        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "fetch_all_collections",
                return_value=rows,
            ),
            mock.patch.object(
                zotero_mcp_server.zotero_write,
                "plan_collection_reconcile",
                return_value=payload,
            ) as plan,
        ):
            result = zotero_mcp_server.tool_plan_collection_reconcile(
                {"items": records}
            )
        self.assertFalse(result["isError"])
        plan.assert_called_once_with(
            [
                {
                    "item_key": "SUZCN22Y",
                    "add_collection_keys": ["GLIOMA01"],
                    "remove_collection_keys": ["AGING001"],
                }
            ],
            allow_no_collections=False,
            collection_keys={"ROOT0001", "GLIOMA01", "AGING001"},
        )

    def test_apply_collection_reconcile_forwards_confirmation(self):
        records = [
            {
                "item_key": "SUZCN22Y",
                "add_collection_keys": [],
                "remove_collection_keys": ["XM2WM4D4"],
            }
        ]
        payload = {
            "total": 1,
            "summary": {"collections_reconciled": 1},
            "results": [],
        }
        with mock.patch.object(
            zotero_mcp_server.zotero_write,
            "execute_collection_reconcile",
            return_value=payload,
        ) as execute:
            result = zotero_mcp_server.tool_apply_collection_reconcile(
                {"items": records, "confirm": True}
            )
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        execute.assert_called_once_with(
            records,
            True,
            allow_no_collections=False,
        )

    def test_plan_pdf_attachment_delete_forwards_scope(self):
        payload = {"total": 1, "summary": {"ready": 1}, "results": []}
        with mock.patch.object(
            zotero_mcp_server.zotero_write,
            "plan_pdf_attachment_delete",
            return_value=payload,
        ) as plan:
            result = zotero_mcp_server.tool_plan_pdf_attachment_delete(
                {
                    "collection_key": "F2MXAUJE",
                    "recursive": True,
                    "limit": 300,
                    "offset": 50,
                    "page_size": 25,
                }
            )
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        plan.assert_called_once_with(
            "F2MXAUJE",
            recursive=True,
            limit=300,
            offset=50,
            page_size=25,
        )

    def test_plan_pdf_attachment_delete_resolves_collection_reference(self):
        payload = {"total": 0, "summary": {}, "results": []}
        rows = [
            {
                "key": "F2MXAUJE",
                "data": {
                    "key": "F2MXAUJE",
                    "name": "Old PDFs",
                    "parentCollection": False,
                },
            }
        ]
        with (
            mock.patch.object(
                zotero_mcp_server.zotero_local,
                "fetch_all_collections",
                return_value=rows,
            ) as fetch,
            mock.patch.object(
                zotero_mcp_server.zotero_write,
                "plan_pdf_attachment_delete",
                return_value=payload,
            ) as plan,
        ):
            result = zotero_mcp_server.tool_plan_pdf_attachment_delete(
                {"collection": {"name": "Old PDFs"}}
            )
        self.assertFalse(result["isError"])
        fetch.assert_called_once_with()
        plan.assert_called_once_with(
            "F2MXAUJE",
            recursive=False,
            limit=1000,
            offset=0,
            page_size=50,
            collections=rows,
        )

    def test_apply_pdf_attachment_delete_forwards_guards(self):
        records = [{"parent_item_key": "PARENT01", "attachment_key": "ATTACH01"}]
        payload = {"total": 1, "summary": {"attachment_deleted": 1}}
        with mock.patch.object(
            zotero_mcp_server.zotero_write,
            "execute_pdf_attachment_delete",
            return_value=payload,
        ) as execute:
            result = zotero_mcp_server.tool_apply_pdf_attachment_delete(
                {
                    "collection_key": "F2MXAUJE",
                    "items": records,
                    "backup_dir": "/tmp/zotero-backup",
                    "confirm": True,
                }
            )
        self.assertFalse(result["isError"])
        execute.assert_called_once_with(
            records,
            "F2MXAUJE",
            "/tmp/zotero-backup",
            True,
            recursive=False,
            allow_shared_parents=False,
            allow_annotations=False,
        )


if __name__ == "__main__":
    unittest.main()
