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
    def test_declined_mcp_write_has_no_database_side_effect(self):
        from lg_cli.backend import ProcessHost
        from lg_cli.workflow_control import WorkflowControl
        async def exercise(root):
            control=WorkflowControl(ProcessHost(lambda *_:None))
            reviewed=[]
            async def deny(details):
                reviewed.append(details)
                return {'decision':'decline'}
            control.approval_handler=deny
            socket=await control.endpoint()
            parameters=StdioServerParameters(command=sys.executable,
                args=['-m','lg_cli.writing_mcp','--workspace',str(root),'--control-socket',socket],
                env={'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'lg-cli')})
            try:
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        result=await session.call_tool('create_chapter',{'slug':'denied','title':'Denied',
                            'content':'Should not be saved.','sequence':1,'book_overview':'A test story.'})
                        self.assertTrue(result.isError)
                self.assertTrue(reviewed, result.content)
                self.assertEqual(reviewed[0]['tool'],'create_chapter')
                self.assertEqual(ProjectStore(root).list_documents(),[])
                self.assertFalse((root/'ReferenceLibrary/bible/book-overview.json').exists())
            finally:
                await control.close()
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            ProjectStore(root).initialize()
            asyncio.run(exercise(root))

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
        from tests.test_story_index import example_graph
        async def exercise(root, original):
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "lg_cli.writing_mcp", "--workspace", str(root)],
                env={"HOME": str(root), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / 'lg-cli')},
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
                            "story_index": example_graph(),
                            "book_overview": "林舟追寻旧信的主人，揭开一段被隐藏的往事。",
                        },
                    )
                    self.assertFalse(response.isError)
                    from lg_cli.book_overview import read_overview
                    self.assertEqual(read_overview(root), "林舟追寻旧信的主人，揭开一段被隐藏的往事。")
                    revision_id = json.loads(response.content[0].text)["id"]
                    from lg_cli.story_index import build_index
                    self.assertEqual(build_index(ProjectStore(root))['characters'][0]['name'], '林舟')
                    stale = await session.call_tool(
                        "edit_manuscript",
                        {
                            "slug": "chapter-1",
                            "content": "stale",
                            "expected_version_id": original.active_version_id,
                            "reason": "stale",
                            "book_overview": "不应保存的简介",
                        },
                    )
                    self.assertTrue(stale.isError)
                    self.assertNotEqual(read_overview(root), "不应保存的简介")
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
