import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.bookshelf import Bookshelf, restore_location
from lg_cli.config import load_config
from lg_cli.credentials import read_profiles, save_profile
from lg_cli.desktop import DesktopError, control, inventory


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'HOME': str(self.root), 'LG_DESKTOP_FIXTURE_KEY': 'synthetic-desktop-test'})
        env.start()
        self.addCleanup(env.stop)
        self.shelf = Bookshelf(self.root / 'checkout/Projects')
        self.first = self.shelf.create('First')
        self.second = self.shelf.create('Second')
        save_profile('sandbox', 'one', 'synthetic-desktop-test', provider='stepfun', model='fixture-one')

    def test_discovers_real_shelf_and_remembers_without_copying_books(self):
        data = inventory(self.root / 'checkout')
        self.assertEqual(data['shelf'], str(self.shelf.root))
        self.assertEqual({b['workspace'] for b in data['books']}, {str(self.first), str(self.second)})
        control(self.shelf.root, None, {'action': 'remember', 'workspace': str(self.second)})
        self.assertEqual(restore_location(self.root), self.second)
        self.assertEqual(inventory(self.shelf.root)['workspace'], str(self.second))
        self.assertEqual(inventory(self.first)['workspace'], str(self.first))

    def test_ui_and_actual_cli_share_model_selection(self):
        control(self.first, None, {'action': 'add', 'name': 'two', 'provider': 'stepfun',
                'model': 'fixture-two', 'base_url': 'https://api.stepfun.com', 'key_env': 'LG_DESKTOP_FIXTURE_KEY'})
        self.assertEqual(load_config(self.first).default_model, 'fixture-two')
        control(self.first, None, {'action': 'use', 'name': 'one'})
        self.assertEqual(load_config(self.second).default_model, 'fixture-one')
        result = subprocess.run([sys.executable, '-m', 'lg_cli', '-C', str(self.first), 'auth', 'model', 'fixture-cli'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(inventory(self.second)['model'], 'fixture-cli')
        control(self.second, None, {'action': 'model', 'name': 'two', 'model': 'fixture-ui'})
        self.assertEqual(load_config(self.first).default_model, 'fixture-ui')

    def test_no_secrets_in_desktop_output_or_error(self):
        output = json.dumps(inventory(self.first))
        self.assertNotIn('synthetic-desktop-test', output)
        self.assertNotIn('"key"', output)
        request = {'action': 'add', 'name': 'bad', 'provider': 'stepfun', 'model': 'fixture',
                   'base_url': 'https://api.stepfun.com', 'key_env': 'MISSING_DESKTOP_KEY'}
        result = subprocess.run([sys.executable, '-m', 'lg_cli.desktop', '--workspace', str(self.first)],
                                input=json.dumps(request), capture_output=True, text=True, timeout=20)
        self.assertEqual(json.loads(result.stdout), {'ok': False, 'error': 'KEY_ENV_MISSING'})
        self.assertEqual(read_profiles('sandbox')['active'], 'one')
        with self.assertRaises(DesktopError):
            control(self.first, None, {'action': 'add', 'key': 'never-accept-a-secret-in-json'})

    def test_failed_configuration_does_not_replace_active_profile(self):
        for url in ('http://remote.example/v1', 'https://user:pass@example.com', 'https://example.com?key=secret'):
            with self.assertRaises(ValueError):
                control(self.first, None, {'action': 'add', 'name': 'bad', 'provider': 'openai',
                    'model': 'fixture', 'base_url': url, 'key_env': 'LG_DESKTOP_FIXTURE_KEY'})
            self.assertEqual(read_profiles('sandbox')['active'], 'one')
        with self.assertRaises(ValueError):
            control(self.first, None, {'action': 'use', 'name': 'missing'})

    def test_switching_profiles_keeps_all_saved_keys_out_of_core_environment(self):
        from lg_cli.core_adapter import _adapter_env
        save_profile('sandbox','two','synthetic-second-key',provider='stepfun',model='fixture-two')
        with patch.dict(os.environ, {'CUSTOM_OLD_KEY':'synthetic-desktop-test', 'CUSTOM_CURRENT_KEY':'synthetic-second-key'}):
            env = _adapter_env(load_config(self.first))
        self.assertNotIn('CUSTOM_OLD_KEY', env)
        self.assertNotIn('CUSTOM_CURRENT_KEY', env)
        self.assertNotIn('LG_DESKTOP_FIXTURE_KEY', env)

    def test_different_shelf_and_environment_remain_separate(self):
        other = Bookshelf(self.root / 'elsewhere').create('Other')
        with self.assertRaises(ValueError):
            control(self.shelf.root, None, {'action': 'remember', 'workspace': str(other)})
        data = inventory(self.first, 'isolated')
        self.assertEqual(data['profiles'], [])
        self.assertEqual(data['active'], '')


if __name__ == '__main__':
    unittest.main()
