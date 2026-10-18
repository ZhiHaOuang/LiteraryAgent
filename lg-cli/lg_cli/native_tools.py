"""One task owns the MCP client context and its child-process lifetime."""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class NativeTools:
    def __init__(self, config, control_socket, *, review_only=False):
        self.config, self.control_socket, self.review_only = config, control_socket, review_only
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.worker = None
        self.tools = {}

    async def start(self):
        self.worker = asyncio.create_task(self._serve())
        await self.ready

    async def _serve(self):
        env = {key: value for key, value in os.environ.items() if key in
            {'PATH', 'HOME', 'SYSTEMROOT', 'TMPDIR', 'LITERARYGIANT_REGISTRY_HOME'}}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        args = ['-m', 'lg_cli.writing_mcp', '--workspace', str(self.config.workspace)]
        if self.config.environment:
            args += ['--environment', self.config.environment]
        if self.control_socket:
            args += ['--control-socket', self.control_socket]
        else:
            args += ['--require-elicitation']
        params = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=self.config.workspace)
        try:
            # Drain child diagnostics without publishing provider or private book data.
            with open(os.devnull, 'w') as errors:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        from .mcp_approval import READ_TOOLS, WRITE_TOOLS
                        allowed = READ_TOOLS if self.review_only else READ_TOOLS | WRITE_TOOLS | {'workflow_status', 'stop_workflow', 'retry_workflow'}
                        self.tools = {tool.name: tool for tool in (await session.list_tools()).tools if tool.name in allowed}
                        self.ready.set_result(None)
                        while True:
                            name, arguments, future = await self.queue.get()
                            try:
                                result = await session.call_tool(name, arguments)
                                if not future.done():
                                    future.set_result(result)
                            except Exception as exc:
                                if not future.done():
                                    future.set_exception(ValueError('LG tool process failed: ' + type(exc).__name__))
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(ValueError('LG tool process could not initialize: ' + type(exc).__name__))
            raise

    async def call(self, name, arguments):
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((name, arguments, future))
        return await future

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
