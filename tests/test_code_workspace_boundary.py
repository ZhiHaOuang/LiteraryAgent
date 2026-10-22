import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.core_adapter import CodexStageExecutor
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config


class CodeWorkspaceBoundaryTests(unittest.TestCase):
    def test_nested_and_symlinked_book_directories_are_rejected_without_writes(self):
        from lg_cli.core_adapter import ensure_code_workspace
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            book = root / 'book'
            ProjectStore(book).initialize(name='Synthetic book')
            nested = book / 'notes' / 'code'
            nested.mkdir(parents=True)
            alias = root / 'alias'
            alias.symlink_to(nested, target_is_directory=True)
            before = sorted(str(p.relative_to(book)) for p in book.rglob('*'))
            for workspace in (book, nested, alias):
                with self.subTest(workspace=workspace):
                    with self.assertRaisesRegex(ValueError, 'book workspace'):
                        ensure_code_workspace(workspace)
            self.assertEqual(before, sorted(str(p.relative_to(book)) for p in book.rglob('*')))
            self.assertFalse((nested / '.literarygiant').exists())
            # Siblings/parents are not scanned recursively, nor treated as the book.
            ensure_code_workspace(root)

    def test_code_rejects_initialized_book_before_runtime_probe_or_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ProjectStore(root).initialize(name='Synthetic book')
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch('lg_cli.core_adapter.discover_core_commands', side_effect=AssertionError('runtime probe')), \
                    patch('lg_cli.provider_transport.provider_bridge', side_effect=AssertionError('provider')):
                result = CodexStageExecutor().run(prompt='test', instructions='test', config=make_config(root), mode='code')
            self.assertFalse(result.ok)
            self.assertIn('book workspace', result.error)
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_regular_code_workspace_still_reaches_runtime_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / '.literarygiant').mkdir()
            with patch('lg_cli.core_adapter.discover_core_commands', return_value=[]) as probe:
                result = CodexStageExecutor().run(prompt='test', instructions='test', config=make_config(root), mode='code')
            probe.assert_called_once()
            self.assertNotIn('book workspace', result.error)
