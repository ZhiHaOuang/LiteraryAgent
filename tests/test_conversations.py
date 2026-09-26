import asyncio
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lg_cli.config import ConfigError, load_config
from lg_cli.conversation_store import ConversationStore
from lg_cli.main import interactive_loop, main
from lg_cli.workflow_runner import WorkflowExecutionResult


class ConversationTests(unittest.TestCase):
    def test_edit_and_bible_curation_stay_in_structured_tui(self):
        session = Mock()
        session.run_command = AsyncMock(return_value=0)

        def run(handler, *args):
            async def exercise():
                await handler("/edit outline --mode rewrite fix continuity")
                await handler("/bible curate outline")
                return 0
            return asyncio.run(exercise())

        session.run.side_effect = run
        with patch("lg_cli.main.LiteraryInput", return_value=session), patch(
            "lg_cli.main.run_in_terminal"
        ) as external:
            interactive_loop(load_config(self.root / "book", environment="sandbox"))
            external.assert_not_called()
        self.assertEqual(session.run_command.call_count, 2)
        for call in session.run_command.call_args_list:
            self.assertTrue(call.kwargs["structured"])
            self.assertIn("--json", call.args[0])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_project_isolation_full_history_and_bounded_context(self):
        first = ConversationStore(self.root / "one")
        second = ConversationStore(self.root / "two")
        key = first.create()
        first.append(key, "user", "The hero is named Lin.")
        first.append(key, "assistant", "A" * 30000)
        first.append(key, "user", "Continue from the tower.")
        self.assertEqual(len(first.read(key)[1]["text"]), 30000)
        self.assertLessEqual(len(first.context(key, 800)), 800)
        self.assertIn("Continue from the tower", first.context(key, 800))
        self.assertEqual(second.list(), [])
        with self.assertRaises(FileNotFoundError):
            second.read(key)
        with self.assertRaises(ValueError):
            first.read("../../outside")
        self.assertEqual(first.path(key).stat().st_mode & 0o777, 0o600)

    def test_restored_conversation_is_passed_to_model_and_saved(self):
        book = self.root / "book"
        store = ConversationStore(book)
        key = store.create()
        store.append(key, "user", "Our hero is Lin, never rename her.")
        store.append(key, "assistant", "Lin reaches the tower.")
        result = WorkflowExecutionResult(
            "chat_workflow", "run-1", "completed", "Lin opens the door.", None, None, 0
        )
        with (
            patch("lg_cli.main.WorkflowRunner") as runner,
            redirect_stdout(io.StringIO()),
        ):
            runner.return_value.run.return_value = result
            self.assertEqual(
                main(
                    [
                        "-C",
                        str(book),
                        "--environment",
                        "sandbox",
                        "--conversation",
                        key,
                        "chat",
                        "What happens next?",
                    ]
                ),
                0,
            )
            request = runner.return_value.run.call_args.args[1]
            self.assertIn("never rename her", request)
            self.assertIn("Lin reaches the tower", request)
            self.assertTrue(request.endswith("What happens next?"))
        records = store.read(key)
        self.assertEqual(
            [r["role"] for r in records], ["user", "assistant", "user", "assistant"]
        )
        self.assertEqual(records[-2]["text"], "What happens next?")
        self.assertEqual(records[-1]["run_id"], "run-1")

    def test_open_switches_workspace_history_and_conversation(self):
        first, second = self.root / "first book", self.root / "second book"
        session = Mock()

        async def command(argv, **kwargs):
            with redirect_stdout(io.StringIO()):
                return main(argv[4:])

        session.run_command.side_effect = command

        def run(handler, model, provider):
            async def exercise():
                await handler(f'/open "{first}"')
                await handler("/chat --dry-run alpha")
                saved = ConversationStore(first).list()[0][0]
                await handler("/new")
                await handler(f"/resume {saved}")
                self.assertTrue(
                    any(
                        call.kwargs.get("role") == "user" and call.args[0] == "alpha"
                        for call in session.append.call_args_list
                    )
                )
                await handler(f'/open "{second}"')
                await handler("/chat --dry-run beta")
                await handler(f'/focus "{first}"')
                await handler("/chat --dry-run new-discussion")
                self.assertFalse(await handler("/exit"))
                return 0

            return asyncio.run(exercise())

        session.run.side_effect = run
        with patch("lg_cli.main.LiteraryInput", return_value=session):
            self.assertEqual(
                interactive_loop(
                    load_config(self.root / "startup", environment="sandbox")
                ),
                0,
            )
        for book, request in ((first, "alpha"), (second, "beta")):
            self.assertFalse((book / ".literarygiant/memory/STORY_BIBLE.md").exists())
            self.assertTrue((book / "ReferenceLibrary/README.md").is_file())
            self.assertTrue((book / ".literarygiant/story.sqlite3").is_file())
            store = ConversationStore(book)
            self.assertEqual(len(store.list()), 2 if book == first else 1)
            self.assertIn(
                request, [store.read(key)[0]["text"] for key, _ in store.list()]
            )
        histories = [call.args[0] for call in session.set_history.call_args_list]
        self.assertEqual(
            histories,
            [book / ".literarygiant/history" for book in (first, second, first)],
        )

    def test_external_writing_path_rejected_but_reference_allowed(self):
        book = self.root / "book"
        config = book / ".literarygiant/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[paths]\noutput="../../outside"\n', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(book, environment="sandbox")
        config.write_text('[paths]\nreference="../../reference"\n', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(book, environment="sandbox")
        (config.parent / "agent.toml").write_text(
            "[context]\nallow_external_reference=true\n", encoding="utf-8"
        )
        self.assertTrue(
            load_config(book, environment="sandbox").output_path.is_relative_to(book)
        )
