from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.project_store import ProjectStore, ProjectStoreError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class WritingToolsTests(unittest.TestCase):
    def test_active_edit_keeps_history_and_rejects_stale_version(self):
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"LITERARYGIANT_REGISTRY_HOME": raw}),
        ):
            store = ProjectStore(Path(raw))
            store.initialize(name="Test Novel")
            original = store.create_document(
                kind="chapter", slug="chapter-1", title="One", content="old"
            )
            revision = store.edit_active_document(
                "chapter-1",
                content="new",
                expected_version_id=original.active_version_id,
                reason="rewrite",
            )
            self.assertEqual(store.get_document("chapter-1").content, "new")
            self.assertEqual(store.get_version("chapter-1", 1).content, "old")
            with self.assertRaises(ProjectStoreError):
                store.edit_active_document(
                    "chapter-1",
                    content="stale",
                    expected_version_id=original.active_version_id,
                    reason="stale edit",
                )
            self.assertEqual(
                store.get_document("chapter-1").active_version_id, revision.id
            )
            self.assertEqual(len(store.list_versions("chapter-1")), 2)

    def test_stdio_tools_edit_and_keep_canon(self):
        async def exercise(root, original):
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "lg_cli.writing_mcp", "--workspace", str(root)],
                env={"HOME": str(root)},
            )
            async with stdio_client(parameters) as (read, write):  # noqa: SIM117 -- keep the transport and session lifetimes explicit
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertIn("project_memory", [t.name for t in tools.tools])
                    response = await session.call_tool(
                        "read_document", {"slug": "chapter-1"}
                    )
                    self.assertFalse(response.isError)
                    self.assertEqual(
                        json.loads(response.content[0].text)["content"], "old"
                    )
                    response = await session.call_tool(
                        "edit_manuscript",
                        {
                            "slug": "chapter-1",
                            "content": "new",
                            "expected_version_id": original.active_version_id,
                            "reason": "author requested",
                        },
                    )
                    self.assertFalse(response.isError)
                    revision_id = json.loads(response.content[0].text)["id"]
                    stale = await session.call_tool(
                        "edit_manuscript",
                        {
                            "slug": "chapter-1",
                            "content": "stale",
                            "expected_version_id": original.active_version_id,
                            "reason": "stale",
                        },
                    )
                    self.assertTrue(stale.isError)
                    diff = await session.call_tool(
                        "document_diff",
                        {"slug": "chapter-1", "before_version": 1, "after_version": 2},
                    )
                    self.assertFalse(diff.isError)
                    self.assertIn("-old", diff.content[0].text)
                    restored = await session.call_tool(
                        "restore_manuscript",
                        {
                            "slug": "chapter-1",
                            "version_number": 1,
                            "expected_version_id": revision_id,
                        },
                    )
                    self.assertFalse(restored.isError)
                    proposal = await session.call_tool(
                        "propose_setting",
                        {
                            "category": "world",
                            "key": "sun",
                            "value": "two suns",
                            "rationale": "discuss conflict",
                        },
                    )
                    self.assertFalse(proposal.isError)

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"LITERARYGIANT_REGISTRY_HOME": raw}),
        ):
            store = ProjectStore(Path(raw))
            store.initialize(name="Test Novel")
            original = store.create_document(
                kind="chapter", slug="chapter-1", title="One", content="old"
            )
            fact = store.set_fact(
                category="world", key="sun", value="one sun", state="canonical"
            )
            asyncio.run(exercise(Path(raw), original))
            self.assertEqual(store.get_document("chapter-1").content, "old")
            self.assertEqual(len(store.list_versions("chapter-1")), 3)
            self.assertEqual(store.get_fact(fact.id).value, "one sun")
            self.assertEqual(len(store.list_proposals()), 1)
