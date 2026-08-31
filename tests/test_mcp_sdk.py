import os
import sys
import unittest
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpSdkIntegrationTests(unittest.TestCase):
    def test_stdio_default_hides_and_blocks_non_literature_tools(self):
        repository = Path(__file__).resolve().parents[1]

        async def run():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "zotero_mcp.zotero_mcp_server"],
                cwd=repository,
                env={**os.environ, "ZOTERO_MCP_DISABLE_PRIVATE": "1"},
            )
            async with (
                stdio_client(params) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                listed = await session.list_tools()
                hidden = await session.call_tool(
                    "zotero_get_annotations",
                    arguments={"key": "ITEM0001"},
                )
                return listed, hidden

        listed, hidden = anyio.run(run)
        names = [tool.name for tool in listed.tools]
        self.assertEqual(len(names), 14)
        self.assertIn("zotero_plan_paper_import", names)
        self.assertNotIn("zotero_get_annotations", names)
        self.assertTrue(hidden.isError)
        self.assertEqual(hidden.content[0].text, "Unknown tool: zotero_get_annotations")

    def test_stdio_round_trip_lists_tools_and_validates_input(self):
        repository = Path(__file__).resolve().parents[1]

        async def run():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "zotero_mcp.zotero_mcp_server", "--toolsets", "all"],
                cwd=repository,
                env={**os.environ, "ZOTERO_MCP_DISABLE_PRIVATE": "1"},
            )
            async with (
                stdio_client(params) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                initialized = await session.initialize()
                listed = await session.list_tools()
                invalid = await session.call_tool("zotero_item", arguments={})
                invalid_apply_reference = await session.call_tool(
                    "zotero_apply_paper_import",
                    arguments={
                        "items": [
                            {
                                "title": "Paper",
                                "collections": [{"name": "Glioma"}],
                            }
                        ],
                        "confirm": False,
                    },
                )
                return initialized, listed, invalid, invalid_apply_reference

        (
            initialized,
            listed,
            invalid,
            invalid_apply_reference,
        ) = anyio.run(run)
        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(initialized.serverInfo.name, "zotero_mcp")
        self.assertEqual(len(tools), 28)
        self.assertIn("zotero_apply_pdf_attachment_delete", tools)
        self.assertIn("zotero_apply_manual_translation_rename", tools)
        self.assertIn("zotero_web_api_status", tools)
        self.assertIn("zotero_resolve_collection", tools)
        self.assertIsNotNone(tools["zotero_item"].outputSchema)
        self.assertTrue(invalid.isError)
        self.assertIn("Input validation error", invalid.content[0].text)
        self.assertTrue(invalid_apply_reference.isError)
        self.assertIn("Input validation error", invalid_apply_reference.content[0].text)


if __name__ == "__main__":
    unittest.main()
