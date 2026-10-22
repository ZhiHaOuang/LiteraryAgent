import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.configure_zhipu import configure, hidden_key, main
from lg_cli.config import load_config
from lg_cli.credentials import environment_dir, read_profiles


class ZhipuSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = patch('pathlib.Path.home', return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)

    def test_one_key_saves_two_bound_profiles_and_selects_native_air(self):
        value = configure('synthetic-private-value', 'test-zhipu')
        self.assertNotIn('synthetic-private-value', json.dumps(value))
        self.assertEqual(value['credentialMode'], '0o600')
        saved = read_profiles('test-zhipu')
        self.assertEqual(saved['active'], 'zhipu-air')
        self.assertEqual({p['model'] for p in saved['profiles'].values()}, {'glm-4.5-air', 'glm-4.6v'})
        self.assertTrue(all(p['key'] == 'synthetic-private-value' for p in saved['profiles'].values()))
        self.assertEqual(saved['profiles']['zhipu-vision']['protocol'], 'responses')
        self.assertEqual(saved['profiles']['zhipu-vision']['base_url'], 'https://open.bigmodel.cn/api/v1')
        config = load_config(self.root / 'no-book', environment='test-zhipu')
        self.assertEqual((config.default_model, config.runtime_engine, config.protocol), ('glm-4.5-air', 'native', 'anthropic'))
        self.assertEqual(stat.S_IMODE(environment_dir('test-zhipu').stat().st_mode), 0o700)

    def test_existing_profile_is_never_replaced_or_prompted(self):
        configure('original-synthetic', 'test-zhipu')
        before = (environment_dir('test-zhipu') / 'credentials.json').read_bytes()
        with patch('scripts.configure_zhipu.hidden_key') as prompt, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--environment', 'test-zhipu']), 1)
        prompt.assert_not_called()
        self.assertEqual(before, (environment_dir('test-zhipu') / 'credentials.json').read_bytes())

    def test_cancel_and_secret_subprocess_output_are_not_logged(self):
        output = io.StringIO()
        result = subprocess.CompletedProcess([], 1, 'SYNTHETIC_SECRET', 'SYNTHETIC_SECRET')
        with patch('scripts.configure_zhipu.subprocess.run', return_value=result), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(main(['--dialog', '--environment', 'test-zhipu']), 1)
        self.assertNotIn('SYNTHETIC_SECRET', output.getvalue())
        self.assertFalse((environment_dir('test-zhipu') / 'credentials.json').exists())

    def test_existing_runtime_file_is_preserved_before_key_input(self):
        location = environment_dir('test-zhipu')
        location.mkdir(parents=True)
        path = location / 'config.toml'
        path.write_text('[runtime]\nengine = "codex"\n')
        with patch('scripts.configure_zhipu.hidden_key') as prompt, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--environment', 'test-zhipu']), 1)
        prompt.assert_not_called()
        self.assertEqual(path.read_text(), '[runtime]\nengine = "codex"\n')

    def test_empty_key_does_not_create_credential_space(self):
        with self.assertRaises(ValueError):
            configure('', 'test-zhipu')
        self.assertFalse(environment_dir('test-zhipu').exists())
