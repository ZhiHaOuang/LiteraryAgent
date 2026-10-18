from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from lg_cli.app_server import launch as launch_app_server
from lg_cli.auth_ui import _add, profile_label
from lg_cli.config import API_ONLY_MESSAGE, load_config
from lg_cli.core_adapter import CodexExecAdapter, _adapter_env, _exec_args
from lg_cli.credentials import environment_dir, read_profiles, save_profile
from lg_cli.live_agent import LiveAgent
from lg_cli.main import main
from lg_cli.native import launch_native
from tests.helpers import make_config


class SubscriptionMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": tmp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def legacy_profile(self):
        directory = environment_dir('test')
        directory.mkdir(parents=True)
        profile = {'provider': 'codex', 'auth_mode': 'chatgpt', 'model': 'old-model'}
        path = directory / 'credentials.json'
        path.write_text(json.dumps({'version': 2, 'active': 'old', 'profiles': {'old': profile}}))
        cache = directory / 'codex-auth' / 'old' / 'auth.json'
        cache.parent.mkdir(parents=True)
        cache.write_text('{"synthetic-cache": true}')
        return path, cache

    def test_old_profile_is_readable_but_never_treated_as_usable_auth(self):
        path, cache = self.legacy_profile()
        before = path.read_bytes(), cache.read_bytes()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated-key'}):
            config = load_config(self.root, environment='test')
        self.assertIsNone(config.api_key)
        self.assertFalse(config.credentials_configured)
        self.assertIn(API_ONLY_MESSAGE, config.warnings)
        with redirect_stdout(io.StringIO()) as status:
            self.assertEqual(main(['--environment', 'test', '-C', str(self.root), 'auth', 'status']), 1)
        self.assertIn(API_ONLY_MESSAGE, status.getvalue())
        with self.assertRaisesRegex(ValueError, 'API-only'):
            save_profile('test', 'old')
        with self.assertRaisesRegex(ValueError, 'API-only'):
            save_profile('test', 'old', 'new-key', replace=True, provider='openai', model='api-model')
        self.assertEqual(before, (path.read_bytes(), cache.read_bytes()))
        data = read_profiles('test')
        self.assertIn('unsupported', profile_label('old', data['profiles']['old'], data))
        save_profile('test', 'api', 'key', provider='openai', model='api-model')
        self.assertEqual(load_config(self.root, environment='test').profile_name, 'api')
        self.assertIn('old', read_profiles('test')['profiles'])
        self.assertEqual(cache.read_bytes(), before[1])

    def test_legacy_login_options_fail_before_any_process_or_write(self):
        process = Mock(side_effect=AssertionError('Must not launch login'))
        with patch('subprocess.Popen', process), patch('subprocess.run', process):
            for options in ([], ['--browser'], ['--model', 'old-model']):
                with self.subTest(options=options), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main(['--environment', 'test', 'auth', 'login', *options]), 1)
                self.assertIn(API_ONLY_MESSAGE, error.getvalue())
        process.assert_not_called()
        self.assertFalse(environment_dir('test').exists())

    def test_cli_and_provider_menu_offer_only_api_providers(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['auth', 'providers']), 0)
        self.assertNotIn('subscription', output.getvalue())
        dialog = Mock()
        dialog.run.return_value = None
        with patch('lg_cli.auth_ui.radiolist_dialog', return_value=dialog) as choose:
            _add('test', self.root)
        self.assertNotIn('codex', dict(choose.call_args.kwargs['values']))

    def test_all_codex_launch_surfaces_reject_subscription_before_discovery(self):
        config = replace(make_config(self.root), auth_mode='chatgpt')
        with patch('lg_cli.core_adapter.discover_core_commands') as discover:
            result = CodexExecAdapter().run(prompt='write', config=config, mode='write')
        discover.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn('API-only', result.error)
        for call in (
            lambda: _adapter_env(config),
            lambda: _exec_args(config=config, mode='write', model_profile=None, final_path=self.root/'out', output_schema=None),
            lambda: launch_native(config),
            lambda: launch_app_server(config, self.root / "runtime.json"),
        ):
            with self.assertRaisesRegex(ValueError, 'API-only'):
                call()


class LiveSubscriptionMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_process_override_does_not_bypass_auth_policy(self):
        config = replace(make_config(Path('/synthetic-book')), auth_mode='chatgpt')
        agent = LiveAgent(config, lambda *_: None, command=['must-not-run'])
        with patch('asyncio.create_subprocess_exec') as launch:
            with self.assertRaisesRegex(ValueError, 'API-only'):
                await agent.start()
        launch.assert_not_called()
