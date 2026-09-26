import asyncio
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lg_cli.bookshelf import Bookshelf, remember_location, restore_location
from lg_cli.config import load_config
from lg_cli.conversation_store import ConversationStore
from lg_cli.dashboard import render_status_bar
from lg_cli.main import interactive_loop, main
from lg_cli.project_store import ProjectStore


class BookshelfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.shelf = Bookshelf(self.root / "books")
        self.shelf.initialize()

    def test_remembers_book_per_environment_and_respects_local_book(self):
        first = self.shelf.create('First')
        second = self.shelf.create('Second')
        remember_location(first, 'sandbox')
        remember_location(second, 'production')
        self.assertEqual(restore_location(self.root, 'sandbox'), first)
        self.assertEqual(restore_location(self.shelf.root, 'sandbox'), first)
        self.assertEqual(restore_location(self.root, 'production'), second)
        self.assertEqual(restore_location(second, 'sandbox'), second)
        other = Bookshelf(self.root / 'other')
        other.initialize()
        self.assertEqual(restore_location(other.root, 'sandbox'), other.root)

    def test_missing_or_replaced_book_falls_back_to_shelf(self):
        book = self.shelf.create('First')
        remember_location(book)
        moved = self.shelf.root / 'Moved'
        book.rename(moved)
        self.assertEqual(restore_location(self.root), self.shelf.root)
        self.shelf.create('First')
        self.assertEqual(restore_location(self.root), self.shelf.root)

    def test_corrupt_location_is_ignored(self):
        from lg_cli.credentials import environment_dir
        book = self.shelf.create('First')
        remember_location(book)
        path = environment_dir('sandbox') / 'location.json'
        for text in ('invalid', '[]', '{}'):
            path.write_text(text)
            self.assertEqual(restore_location(self.root), self.root)

    def test_empty_legacy_agent_checkout_does_not_override_last_book(self):
        book = self.shelf.create('First')
        remember_location(book)
        checkout = self.root / 'agent'
        entry = checkout / 'lg-cli/lg_cli/main.py'
        entry.parent.mkdir(parents=True)
        entry.write_text('# source checkout')
        store = ProjectStore(checkout)
        store.initialize()
        self.assertEqual(restore_location(checkout), book)
        store.create_document(kind='chapter', slug='one', title='Actual book')
        self.assertEqual(restore_location(checkout), checkout)

    def test_only_bare_interactive_launch_restores_location(self):
        book = self.shelf.create('First')
        with patch('lg_cli.main.restore_location', return_value=book) as restore, \
             patch('lg_cli.main._dispatch', return_value=0), \
             patch('lg_cli.main.sys.stdin.isatty', return_value=True):
            self.assertEqual(main([]), 0)
            restore.assert_called_once()
            restore.reset_mock()
            for args in (['-C', str(book)], ['--shelf', str(self.shelf.root)], ['status']):
                self.assertEqual(main(args), 0)
            restore.assert_not_called()
            with patch('lg_cli.main.sys.stdin.isatty', return_value=False):
                self.assertEqual(main([]), 0)
            restore.assert_not_called()

    def test_creation_is_local_and_preserves_existing_books(self):
        first = self.shelf.create("第一本书")
        second = self.shelf.create("Second Book")
        self.assertEqual(first.parent, self.shelf.root)
        self.assertFalse(ProjectStore(self.shelf.root).initialized)

        self.assertEqual(
            {p["workspace"] for p in self.shelf.books()}, {str(first), str(second)}
        )
        for book in (first, second):
            self.assertTrue((book / ".literarygiant/agent.toml").is_file())
            self.assertTrue((book / ".literarygiant/story.sqlite3").is_file())
        with self.assertRaises(FileExistsError):
            self.shelf.create("Second Book")
        for name in ("../outside", "/absolute", "a/b", "..", ".hidden"):
            with self.assertRaises(ValueError):
                self.shelf.create(name)
        self.assertEqual(Bookshelf.discover(first).root, self.shelf.root)

    def test_other_shelves_and_symlinks_not_in_focus_list(self):
        external = Bookshelf(self.root / "other").create("Other Book")
        (self.shelf.root / "escape").symlink_to(external, target_is_directory=True)
        self.assertEqual(self.shelf.books(), [])
        with self.assertRaises(ValueError):
            self.shelf.check_book(external)

    def test_cli_newbook_and_root_writing_guard(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["--shelf", str(self.shelf.root), "newbook", "CLI Book"]), 0
            )
            self.assertEqual(
                main(["--shelf", str(self.shelf.root), "chat", "--dry-run", "write"]), 1
            )
            self.assertEqual(main(["--shelf", str(self.shelf.root), "init"]), 1)
        self.assertTrue(ProjectStore(self.shelf.root / "CLI Book").initialized)
        self.assertFalse(ProjectStore(self.shelf.root).initialized)

    def test_switch_restores_only_selected_books_latest_conversation(self):
        first = self.shelf.create("One")
        second = self.shelf.create("Two")
        keys = {}
        for book in (first, second):
            store = ConversationStore(book)
            keys[book.name] = store.create()
            store.append(keys[book.name], "user", f"Only {book.name}'s story")
            store.append(keys[book.name], "assistant", f"Reply for {book.name}")
        session = Mock()
        session.run_command = AsyncMock(return_value=0)

        def run(handler, model, provider):
            async def exercise():
                await handler("write a story")
                session.run_command.assert_not_called()
                for book in (first, second, first):
                    session.append.reset_mock()
                    await handler(f'/focus "{book.name}"')
                    texts = [call.args[0] for call in session.append.call_args_list]
                    self.assertIn(f"Only {book.name}'s story", texts)
                    self.assertNotIn(
                        f"Only {'Two' if book == first else 'One'}'s story", texts
                    )
                    await handler("continue")
                    argv = session.run_command.call_args.args[0]
                    self.assertEqual(argv[argv.index("-C") + 1], str(book))
                    self.assertEqual(
                        argv[argv.index("--conversation") + 1], keys[book.name]
                    )
                await handler('/newbook "Three"')
                self.assertTrue(ProjectStore(self.shelf.root / "Three").initialized)
                await handler("continue")
                argv = session.run_command.call_args.args[0]
                self.assertEqual(
                    argv[argv.index("-C") + 1], str(self.shelf.root / "Three")
                )
                return 0

            return asyncio.run(exercise())

        session.run.side_effect = run
        with patch("lg_cli.main.LiteraryInput", return_value=session):
            self.assertEqual(
                interactive_loop(load_config(self.shelf.root, environment="sandbox")), 0
            )
        self.assertFalse(ProjectStore(self.shelf.root).initialized)

    def test_picker_lists_current_shelf_only(self):
        local = self.shelf.create("Local")
        Bookshelf(self.root / "other").create("Foreign")
        session = Mock()
        session.ask = AsyncMock(return_value=None)

        def run(handler, model, provider):
            asyncio.run(handler("/focus"))
            return 0

        session.run.side_effect = run
        with patch("lg_cli.main.LiteraryInput", return_value=session):
            interactive_loop(load_config(self.shelf.root, environment="sandbox"))
        values = session.ask.call_args.kwargs["values"]
        self.assertEqual([key for key, _ in values], [str(local)])

    def test_newbook_prompts_for_missing_shelf_and_creates_in_chosen_directory(self):
        standalone = Bookshelf(self.root / "old").create("Existing")
        destination = self.root / "new shelf"
        session = Mock()
        session.ask = AsyncMock(side_effect=[str(destination), "New Story"])

        def run(handler, model, provider):
            asyncio.run(handler("/newbook"))
            return 0

        session.run.side_effect = run
        with patch("lg_cli.main.LiteraryInput", return_value=session), patch(
            "lg_cli.main.Bookshelf.discover", return_value=None
        ):
            interactive_loop(load_config(standalone, environment="sandbox"))
        target = destination / "New Story"
        self.assertTrue(ProjectStore(target).initialized)
        self.assertFalse(ProjectStore(destination).initialized)
        self.assertTrue(ProjectStore(standalone).initialized)
        session.append.assert_any_call(f"Created book: {target}\n")
        session.set_history.assert_called_with(target / ".literarygiant/history")

    def test_newbook_cancel_does_not_create_shelf(self):
        session = Mock()
        session.ask = AsyncMock(return_value=None)
        session.run.side_effect = lambda handler, *args: asyncio.run(handler("/newbook"))
        with patch("lg_cli.main.LiteraryInput", return_value=session), patch(
            "lg_cli.main.Bookshelf.discover", return_value=None
        ):
            interactive_loop(load_config(self.root / "standalone", environment="sandbox"))
        session.set_history.assert_not_called()

    def test_status_shows_shelf_relative_book_location(self):
        book = self.shelf.create("Story")
        for workspace, expected in ((book, "books / Story"), (self.shelf.root, "books / .")):
            output = render_status_bar(
                load_config(workspace, environment="sandbox"), 80,
                shelf_root=self.shelf.root,
            )
            self.assertIn(expected, output)
            self.assertNotIn(str(self.root), output)
