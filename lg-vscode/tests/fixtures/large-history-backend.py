"""Real LG JSON-RPC peer with a synthetic large conversation, without model calls."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend_stdio import serve
from lg_cli.conversation_store import ConversationStore
from lg_cli.init_project import init_workspace
from tests.helpers import make_config

root = Path(sys.argv[1])
init_workspace(root)
store = ConversationStore(root)
key = store.create()
store.append(key, 'assistant', '中文正文。🖋\n"原稿"' * 160000)
with patch('lg_cli.live_agent.LiveAgent.start', side_effect=AssertionError('History must not start a model')):
    asyncio.run(serve(make_config(root)))
