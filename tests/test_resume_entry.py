from __future__ import annotations

import tempfile
import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lg_cli.main import interactive_loop
from tests.helpers import make_config


class ResumeEntryTests(unittest.TestCase):
    def test_resume_does_not_invoke_model(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"HOME": raw}):
            session = Mock()
            session.run_command = AsyncMock()
            def run(handler, model, provider):
                async def exercise():
                    for value in ("resume", "/resume", "/exit"):
                        await handler(value)
                    return 0
                return asyncio.run(exercise())
            session.run.side_effect = run
            with patch("lg_cli.main.LiteraryInput", return_value=session), patch(
                "lg_cli.main._run_task"
            ) as task, patch("builtins.print") as output:
                self.assertEqual(interactive_loop(make_config(Path(raw))), 0)
                task.assert_not_called()
                session.run_command.assert_not_called()
                self.assertTrue(any("No saved conversations" in str(call) for call in session.append.call_args_list))
