import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.config import load_config
from lg_cli.core_adapter import _exec_args
from lg_cli.init_project import init_workspace
from lg_cli.knowledge import KnowledgeGateway
from lg_cli.memory import read_memory_context
from lg_cli.workflow_runner import WorkflowRunner

from tests.helpers import FakeAdapter, make_config


class ProjectContextTests(unittest.TestCase):
    def test_book_policy_and_memory_do_not_leak_between_projects(self):
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"HOME": raw}),
        ):
            root = Path(raw)
            books = [root / "one", root / "two"]
            global_config = root / ".literarygiant/environments/sandbox/config.toml"
            global_config.parent.mkdir(parents=True)
            global_config.write_text(
                '[paths]\nmemory="/foreign-memory"\n[knowledge]\nlibrary="/foreign-library"\n',
                encoding="utf-8",
            )
            for index, book in enumerate(books):
                init_workspace(book)
                policy = book / ".literarygiant/agent.toml"
                policy.write_text(
                    f'[context]\ninstructions="book-rule-{index}"\nconversation_chars=512\n',
                    encoding="utf-8",
                )
                config = load_config(book, environment="sandbox")
                self.assertTrue(config.memory_path.is_relative_to(book))
                self.assertIsNone(config.library_path)
                self.assertEqual(config.conversation_chars, 512)
                adapter = FakeAdapter()
                result = WorkflowRunner(
                    replace(config, api_key="fake-test-key"), adapter=adapter
                ).run("chat", "continue")
                self.assertTrue(result.ok, result.error)
                prompt = adapter.calls[0]["prompt"]
                self.assertIn(f"book-rule-{index}", prompt)
                self.assertNotIn(f"book-rule-{1 - index}", prompt)
                init_workspace(book)
                self.assertIn(f"book-rule-{index}", policy.read_text())

    def test_parent_library_and_symlinked_memory_are_not_implicitly_loaded(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            book = root / "book"
            abstract = root / "Library/AbstractLibrary"
            abstract.mkdir(parents=True)
            memory = book / ".literarygiant/memory"
            memory.mkdir(parents=True)
            source = root / "other-book-secret.md"
            source.write_text("OTHER_BOOK_SECRET", encoding="utf-8")
            (memory / "STORY_BIBLE.md").symlink_to(source)
            config = make_config(book)
            self.assertIsNone(KnowledgeGateway(config).library_root)
            (book / "Library").symlink_to(root / "Library", target_is_directory=True)
            self.assertIsNone(KnowledgeGateway(config).library_root)
            self.assertNotIn(
                "OTHER_BOOK_SECRET", read_memory_context(book).to_prompt_text()
            )
            self.assertEqual(
                KnowledgeGateway(
                    replace(config, allow_external_reference=True)
                ).library_root,
                root / "Library",
            )

    def test_core_disables_implicit_ancestor_instructions(self):
        args = _exec_args(
            config=make_config(Path("/tmp/book")),
            mode="chat",
            model_profile=None,
            final_path=Path("/tmp/result"),
            output_schema=None,
        )
        self.assertIn("project_doc_max_bytes=0", args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ephemeral", args)
