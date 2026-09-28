"""Local JSON-RPC 2.0 / JSONL transport; stdout contains protocol messages only."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .backend import Backend
from .config import load_config


async def serve(config):
    def write(record):
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    backend = Backend(config, lambda event: write({'jsonrpc': '2.0', 'method': 'event', 'params': event}))
    reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    initialized = False
    tasks = set()
    active_ids = set()
    async def handle(message):
        nonlocal initialized
        key = message['id']
        try:
            method, params = message['method'], message.get('params', {})
            if method == 'initialize':
                if initialized:
                    raise ValueError('Already initialized.')
                initialized = True
            elif not initialized:
                raise ValueError('Initialize first.')
            result = await backend.dispatch(method, params)
            write({'jsonrpc': '2.0', 'id': key, 'result': result})
        except (Exception,) as exc:
            write({'jsonrpc': '2.0', 'id': key, 'error': {'code': -32000, 'message': str(exc)}})
        finally:
            active_ids.discard(key)
    try:
        while line := await reader.readline():
            try:
                message = json.loads(line)
                if (not isinstance(message, dict) or message.get('jsonrpc') != '2.0'
                    or type(message.get('id')) not in (int, str) or not isinstance(message.get('method'), str)
                    or not isinstance(message.get('params', {}), dict)):
                    raise ValueError('Expected a JSON-RPC request object with id, method and object params.')
                if message['id'] in active_ids:
                    raise ValueError('Duplicate in-flight request ID.')
                active_ids.add(message['id'])
            except (ValueError, TypeError) as exc:
                write({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': str(exc)}})
                continue
            task = asyncio.create_task(handle(message))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            await asyncio.sleep(0)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await backend.close()
        transport.close()


def main():
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--environment')
    args = parser.parse_args()
    asyncio.run(serve(load_config(args.workspace.resolve(), environment=args.environment)))


if __name__ == '__main__':
    main()
