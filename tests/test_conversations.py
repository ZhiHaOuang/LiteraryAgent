import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lg_cli.config import ConfigError, load_config
from lg_cli.conversation_store import ConversationStore
from lg_cli.main import interactive_loop, main


class ConversationTests(unittest.TestCase):
    def test_edit_and_bible_curation_stay_in_structured_tui(self):
        session = Mock()
        session.run_command = AsyncMock(return_value=0)
        from lg_cli.init_project import init_workspace
        from lg_cli.project_store import ProjectStore
        init_workspace(self.root / 'book')
        ProjectStore(self.root / 'book').create_document(kind='outline',slug='outline',title='Outline',content='Existing plan.')

        def run(handler, *args):
            async def exercise():
                try:
                    await handler("/edit outline --mode rewrite fix continuity")
                    await handler("/bible curate outline")
                    return 0
                finally:
                    await session.shutdown_handler()
            return asyncio.run(exercise())

        session.run.side_effect = run
        with patch("lg_cli.main.LiteraryInput", return_value=session), patch(
            "lg_cli.main.run_in_terminal"
        ) as external, patch('lg_cli.terminal_backend.TerminalBackendClient.workflow',
            new_callable=AsyncMock,return_value={'status':'completed'}) as workflow:
            interactive_loop(load_config(self.root / "book", environment="sandbox"))
            external.assert_not_called()
        session.run_command.assert_not_called()
        self.assertEqual(workflow.call_count,2)
        edit,curation=(call.args[0] for call in workflow.call_args_list)
        self.assertEqual((edit['command'],edit['document'],edit['revisionMode']),('revise','outline','rewrite'))
        self.assertEqual(edit['request'],'fix continuity')
        self.assertEqual((curation['command'],curation['document']),('bible-curate','outline'))

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
        from tests.helpers import make_config
        from tests.intent_fixture import current_user, routed_reply
        from tests.test_native_runtime import answer
        seen, classified = [], []
        async def model(_model, request, delta):
            latest = current_user(request['input'])
            routed = routed_reply(latest)
            if routed is not None:
                classified.append(request)
                return answer(json.dumps(routed))
            seen.append(request)
            delta('Lin opens the door.')
            return answer('Lin opens the door.')
        config = replace(make_config(book), runtime_engine='native', protocol='responses',
                         default_model='conversation-fixture')
        stdout = io.StringIO()
        with (
            patch('lg_cli.main.load_config', return_value=config),
            patch('lg_cli.native_model.NativeModel.generate', model),
            patch('lg_cli.live_agent.LiveAgent', side_effect=AssertionError('Native must not invoke Codex')),
            redirect_stdout(stdout),
        ):
            self.assertEqual(
                main(
                    [
                        "-C",
                        str(book),
                        "--environment",
                        "sandbox",
                        "--conversation",
                        key,
                        "--json",
                        "What happens next?",
                    ]
                ),
                0,
            )
            self.assertEqual(len(classified), 1)
            self.assertEqual(len(seen), 1)
            request = json.dumps(seen[0], ensure_ascii=False)
            self.assertIn("never rename her", request)
            self.assertIn("Lin reaches the tower", request)
            self.assertTrue(current_user(seen[0]['input']).endswith("What happens next?"))
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['text'], 'Lin opens the door.')
        self.assertEqual(result['session_id'], key)
        records = store.read(key)
        self.assertEqual(
            [r["role"] for r in records], ["user", "assistant", "user", "assistant"]
        )
        self.assertEqual(records[-2]["text"], "What happens next?")
        self.assertEqual(records[-1]["run_id"], result['run_id'])
        self.assertEqual(records[-1]['text'], 'Lin opens the door.')
        self.assertEqual(len(store.list()), 1)

    def test_open_switches_workspace_history_and_conversation(self):
        first, second = self.root / "first book", self.root / "second book"
        session = Mock()

        async def command(argv, **kwargs):
            with redirect_stdout(io.StringIO()):
                return main(argv[4:])

        session.run_command.side_effect = command

        def run(handler, model, provider):
            async def exercise():
                try:
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
                finally:
                    await session.shutdown_handler()

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
            self.assertEqual(len(store.list()), 1)
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
