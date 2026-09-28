"""Local, read-only run navigation; never starts an agent or a workflow."""
import asyncio
from pathlib import Path

STATES = {'running': '进行中', 'completed': '已完成', 'failed': '失败',
    'cancelled': '已停止', 'planned': '仅预览'}


def run_details(record):
    lines = ['任务：' + record.get('request', ''),
        '状态：' + STATES.get(record.get('status'), record.get('status', '未知')),
        '命令：/' + record.get('command', ''),
        '模型：' + record.get('model', '未记录'),
        '最近更新：' + record.get('updated_at', '').replace('T', ' '),
        '已完成步骤：' + ('、'.join(record.get('completed_stages', [])) or '暂无')]
    if record.get('artifact_path'):
        lines.append('结果文件：' + str(record['artifact_path']))
    if record.get('error'):
        lines.append('错误：' + str(record['error']))
    return '\n\n'.join(lines)


async def browse_runs(session, store, run_id=None):
    if run_id is not None and (Path(run_id).name != run_id or run_id in {'.', '..'}):
        raise ValueError('无效的运行记录。')
    while True:
        selected = run_id
        if selected is None:
            records = await asyncio.to_thread(store.list_runs, 200)
            if not records:
                await session.view_text('运行记录', '本书尚无运行记录。')
                return
            try:
                selected = await session.ask(kind='choice', title='运行记录',
                    text='选择查看详情，Esc 返回', record=False,
                    values=[(r['run_id'], (
                        ' '.join(r.get('request', '').split())[:60] or '/' + r.get('command', ''),
                        STATES.get(r.get('status'), '未知') + ' · ' + r.get('updated_at', '')[:16].replace('T', ' ')
                    )) for r in records])
            finally:
                session.close_dialog()
            if selected is None:
                return
        record = await asyncio.to_thread(store.load, selected)
        await session.view_text('运行详情', run_details(record))
        if run_id is not None:
            return
