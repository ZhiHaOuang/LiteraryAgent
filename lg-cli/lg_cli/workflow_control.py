"""Local, session-owned workflow controls exposed to the main writing agent."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import uuid
from pathlib import Path


class WorkflowControl:
    def __init__(self, session):
        self.session = session
        self.approval_handler = None
        self._clients = set()
        self.job = None
        self._server = None
        self._directory = None
        self._background = set()
        self._action_lock = asyncio.Lock()
        self._changed = asyncio.Event()

    @property
    def active(self):
        return self.job is not None and self.job['status'] in {'starting', 'running', 'stopping'}

    def reset(self):
        if self.active:
            raise ValueError('当前工作流尚未结束。')
        self.job = None

    def status(self):
        if self.job is None:
            return {'status': 'idle'}
        return {key: self.job[key] for key in ('id', 'command', 'status', 'attempt', 'guidance', 'returncode')}

    async def endpoint(self):
        if self._server is None:
            self._directory = tempfile.TemporaryDirectory(prefix='lg-control-')
            path = Path(self._directory.name) / 'control.sock'
            self._server = await asyncio.start_unix_server(self._serve, path=str(path), limit=4 * 1024 * 1024)
            path.chmod(0o600)
        return str(Path(self._directory.name) / 'control.sock')

    async def run(self, argv, *, structured=False, env=None, cwd=None, label=None):
        if self.active:
            raise ValueError('当前工作流尚未结束。')
        self.job = {'id': uuid.uuid4().hex, 'command': label or list(argv[4:] if len(argv) > 4 else argv),
            'status': 'starting', 'attempt': 1, 'guidance': '', 'returncode': None,
            'stop_requested': False, 'argv': list(argv), 'structured': structured, 'env': env, 'cwd': cwd}
        return await self._execute(self.job)

    def _publish(self):
        if self.job:
            status = self.job['status']
            labels = {'starting': '准备启动', 'running': '运行中', 'stopping': '正在停止',
                'stopped': '已停止', 'completed': '已完成', 'failed': '失败'}
            self.session.set_task('workflow:' + self.job['id'], name='当前命令',
                status={'stopped': 'cancelled', 'starting': 'running', 'stopping': 'running'}.get(status, status),
                step=f"{self.job['command']} · {labels[status]} · 第 {self.job['attempt']} 次",
                details=[json.dumps(self.status(), ensure_ascii=False, indent=2)])

    async def _execute(self, job):
        job['status'] = 'stopping' if job['stop_requested'] else 'running'
        self._changed.clear()
        self._publish()
        env = job['env']
        if job['guidance']:
            env = dict(os.environ if env is None else env)
            env['LG_WORKFLOW_GUIDANCE'] = job['guidance']
        try:
            kwargs = {'structured': job['structured']}
            if env is not None:
                kwargs['env'] = env
            if job['cwd'] is not None:
                kwargs['cwd'] = job['cwd']
            code = await self.session.run_command(job['argv'], **kwargs)
            job['returncode'] = code
            job['status'] = 'stopped' if job['stop_requested'] else 'completed' if code == 0 else 'failed'
            return code
        except BaseException:
            job['status'] = 'failed'
            raise
        finally:
            self._publish()
            self._changed.set()

    async def stop(self, job_id):
        async with self._action_lock:
            self._require_job(job_id)
            if not self.active:
                return self.status()
            self.job['stop_requested'] = True
            self.job['status'] = 'stopping'
            self._publish()
            # A just-scheduled process may not yet have an OS handle.
            for _ in range(200):
                if self.session.process is not None or not self.active:
                    break
                await asyncio.sleep(.01)
            if self.session.process is not None:
                await self.session.stop_workflow_process()
            await asyncio.wait_for(self._changed.wait(), 12)
            return self.status()

    async def retry(self, job_id, guidance=''):
        async with self._action_lock:
            self._require_job(job_id)
            if self.job['status'] not in {'failed', 'stopped'}:
                raise ValueError('只能重试失败或已停止的工作流，不能重复执行已成功的修改。')
            if len(guidance) > 16000:
                raise ValueError('修改指令过长，请缩短到 16000 字符以内。')
            self.job['attempt'] += 1
            self.job['guidance'] = guidance or self.job['guidance']
            self.job['returncode'] = None
            self.job['stop_requested'] = False
            self.job['status'] = 'starting'
            self._publish()
            self.session.begin_work()
            async def execute():
                animation = asyncio.create_task(self.session._animate_progress())
                try:
                    await self._execute(self.job)
                except Exception as exc:
                    self.session.append(f'工作流重试失败：{exc}\n')
                finally:
                    animation.cancel()
                    self.session.end_work()
                    await asyncio.gather(animation, return_exceptions=True)
            task = asyncio.create_task(execute())
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            return self.status()

    def _require_job(self, job_id):
        if self.job is None or job_id != self.job['id']:
            raise ValueError('工作流标识已过期，请重新查看当前任务。')

    async def cancel_requests(self):
        pending = [task for task in self._clients if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            await self._handle(reader, writer)
        finally:
            self._clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _handle(self, reader, writer):
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 5))
            action = request.get('action')
            if action == 'approval':
                if self.approval_handler is None:
                    raise ValueError('No approval handler connected.')
                result = await self.approval_handler(request.get('details', {}))
            elif action == 'status':
                result = self.status()
            elif action == 'stop':
                result = await self.stop(request.get('job_id'))
            elif action == 'retry':
                result = await self.retry(request.get('job_id'), request.get('guidance', ''))
            else:
                raise ValueError('未知工作流操作。')
            response = {'result': result}
        except asyncio.CancelledError:
            response = {'error': 'Request cancelled; no approval granted.'}
        except (OSError, ValueError, TypeError, asyncio.TimeoutError) as exc:
            response = {'error': str(exc) or '工作流控制超时。'}
        try:
            writer.write((json.dumps(response, ensure_ascii=False) + '\n').encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self):
        await self.cancel_requests()
        if self.active:
            await self.stop(self.job['id'])
        await asyncio.gather(*self._background, return_exceptions=True)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._directory:
            self._directory.cleanup()
            self._directory = None


def control_request(path, action, **params):
    """MCP-side client. It cannot supply arbitrary process arguments or shell code."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(600 if action == "approval" else 25)
        connection.connect(str(path))
        connection.sendall((json.dumps({'action': action, **params}, ensure_ascii=False) + '\n').encode())
        with connection.makefile('r', encoding='utf-8') as stream:
            response = json.loads(stream.readline(4 * 1024 * 1024))
    if 'error' in response:
        raise ValueError(response['error'])
    return response['result']
