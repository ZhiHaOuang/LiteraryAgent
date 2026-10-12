"""Actual CLI process coverage; local reports are fixtures, not model evaluations."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import reference_card


class StudioCLITests(unittest.TestCase):
    def test_reference_library_candidates_through_actual_cli(self):
        root = Path(tempfile.mkdtemp(prefix='lg-library-cli-'))
        workspace = root / 'novel';workspace.mkdir()
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(repo/'lg-cli'), LITERARYGIANT_REGISTRY_HOME=str(root/'registry'))
        evidence=[]
        def call(operation, arguments=None, confirm=False, code=0):
            command=[sys.executable,'-m','lg_cli.main','-C',str(workspace),'studio',operation,'--json']
            if arguments is not None:
                source=root/f'input-{len(evidence):02d}.json';source.write_text(json.dumps(arguments,ensure_ascii=False));command.extend(['--input',str(source)])
            if confirm:command.append('--confirm')
            result=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=45)
            evidence.append({'operation':operation,'confirmed':confirm,'exitCode':result.returncode})
            (root/'commands.json').write_text(json.dumps(evidence,indent=2))
            self.assertEqual(result.returncode,code,f'CLI operation failed; evidence: {root}')
            return json.loads(result.stdout)
        call('initialize',confirm=True)
        args=dict(kind='character',slug='lin',title='林砚',content='原创人物资料：维修学徒选择先断电再救人。',expected_version_id=0,reason='author-generated reference')
        call('reference',args,code=2)
        self.assertEqual(call('library',{'topic':'characters'})['total'],0)
        saved=call('reference',args,confirm=True)
        page=call('library',{'topic':'characters'})
        detail=call('entry',{'topic':'characters','key':page['entries'][0]['key']})
        self.assertIn('维修学徒',detail['body'])
        context=call('action-context',dict(topic='characters',key=detail['key'],action='generate',expected_fingerprint=detail['fingerprint']))
        self.assertIn('不自动采用',context['prompt'])
        pending=call('library',{'topic':'pending'})
        self.assertEqual(pending['entries'][0]['status'],'candidate')
        adopt=dict(slug='lin',version_number=1,expected_version_id=saved['version']['id'])
        call('adopt-reference',adopt,code=2)
        self.assertEqual(call('library',{'topic':'pending'})['total'],1)
        call('adopt-reference',adopt,confirm=True)
        self.assertEqual(call('library',{'topic':'pending'})['total'],0)
        print('CLI library evidence:',root)

    def test_original_chapter_workflow_through_cli_processes(self):
        # Retain this independent project and command evidence for inspection.
        root = Path(tempfile.mkdtemp(prefix='lg-studio-cli-'))
        workspace = root / 'novel'
        workspace.mkdir()
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(repo / 'lg-cli'),
            LITERARYGIANT_REGISTRY_HOME=str(root / 'registry'))
        evidence = []

        def call(operation, arguments=None, *, confirm=False, code=0):
            command = [sys.executable, '-m', 'lg_cli.main', '-C', str(workspace), 'studio', operation, '--json']
            if arguments is not None:
                source = root / f'input-{len(evidence):02d}.json'
                source.write_text(json.dumps(arguments, ensure_ascii=False), encoding='utf-8')
                command.extend(['--input', str(source)])
            if confirm:
                command.append('--confirm')
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            evidence.append({'operation': operation, 'confirmed': confirm, 'exitCode': result.returncode})
            (root / 'commands.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            self.assertEqual(result.returncode, code, f'{operation}: unexpected exit code; evidence: {root}')
            return json.loads(result.stdout)

        self.assertEqual(call('initialize', code=2)['status'], 'confirmation_required')
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertTrue(call('initialize', confirm=True)['initialized'])
        status = call('status')
        profile_version = status['profileVersion']
        stages = [
            ('position', {'positioning': '原创浮城救援悬疑', 'targetWords': 1000000}),
            ('plan', {'volumes': ['一：停钟', '二：潮痕', '三：旧图', '四：归航']}),
            ('world', {'characters': ['许栎：维修师', '岑雨：值守员'],
                'relationships': ['许栎与岑雨共同救援'], 'settings': ['衡湾浮城依靠分区电网供能'],
                'foreshadowing': ['配额记录出现不属于值班人的修改']}),
            ('scene', {'tasks': ['第一章：隔离漏电后救援'], 'pending': ['等待正文审阅']}),
        ]
        for stage, updates in stages:
            arguments = {'stage': stage, 'content': '原创测试规划：隔离故障分区后救出闸门内的工友。',
                'updates': updates, 'expected_profile_version': profile_version}
            self.assertEqual(call('save', arguments, code=2)['status'], 'confirmation_required')
            self.assertEqual(call('status')['profileVersion'], profile_version)
            saved = call('save', arguments, confirm=True)
            profile_version = saved['profileVersion']
            self.assertTrue((workspace / saved['artifact']).is_file())

        prose = '衡湾浮城的钟塔停了。许栎听见闸门后有人呼救，便停下脚步。岑雨举起灯，照见绝缘层裂开的电缆。'
        arguments = {'slug': 'first', 'title': '停钟', 'content': prose, 'sequence': 1,
            'expected_version_id': 0, 'reason': '独立原创 CLI 测试'}
        draft = call('draft', arguments, confirm=True)
        active_id = draft['version']['id']
        revised = prose + '许栎先切断故障分区，再和岑雨拉开闸门。汽笛从远处传来，他知道自己已错过末班返乡船。'
        revision = call('draft', {**arguments, 'content': revised, 'expected_version_id': active_id,
            'reason': '补足隔离电源与救援的动作因果'}, confirm=True)
        candidate = revision['version']['version_number']
        self.assertFalse(revision['diff']['applied'])
        self.assertEqual(call('dashboard')['acceptedWords'], 0)
        accept = {'slug': 'first', 'version_number': candidate, 'expected_version_id': active_id}
        self.assertIn('Missing or stale', call('accept', accept, confirm=True, code=1)['error'])

        context = call('review-context', {'slug': 'first', 'version_number': candidate})
        span = next(iter(context['source_spans']))
        for index, category in enumerate(context['schemas']):
            report = {'summary': '本地测试报告：救援动作具有原文依据。', 'entries': [{
                'instance_card': reference_card(category), 'key': 'rescue', 'label': '主动救援',
                'analysis': '许栎先隔离故障再救人，选择留下使他错过返乡船。',
                'evidence': [{'span_id': span}]}], 'findings': []}
            call('review', {'slug': 'first', 'version_number': candidate,
                'source_fingerprint': context['sourceFingerprint'], 'reports': {category: report}}, confirm=True)
            if index < len(context['schemas']) - 1:
                self.assertIn('Missing or stale', call('accept', accept, confirm=True, code=1)['error'])
        self.assertEqual(call('accept', accept, code=2)['status'], 'confirmation_required')
        self.assertEqual(call('dashboard')['acceptedWords'], 0)
        self.assertTrue(call('accept', accept, confirm=True)['accepted'])
        call('accept', {**accept, 'expected_version_id': revision['version']['id']}, confirm=True, code=1)
        call('overview', {'text': '维修师许栎在浮城救援中发现配额记录异常。'}, confirm=True)
        call('save', {'stage': 'review', 'content': '测试流程已完成，等待作者进一步试读。',
            'updates': {'tasks': ['等待作者进一步试读'], 'pending': []},
            'expected_profile_version': profile_version}, confirm=True)
        book = call('dashboard')
        self.assertEqual(book['targetWords'], 1000000)
        self.assertEqual(book['acceptedWords'], len(''.join(revised.split())))
        self.assertEqual(book['writtenWords'], book['acceptedWords'])
        self.assertEqual(len(book['volumes']), 4)
        for key in ['positioning', 'characters', 'relationships', 'settings', 'foreshadowing', 'overview', 'chapters']:
            self.assertTrue(book[key], key)
        self.assertEqual(book['tasks'], ['等待作者进一步试读'])
        self.assertFalse(any(f'v{candidate} 待审阅/采用' in item for item in book['pending']))
        (root / 'dashboard.json').write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'CLI workflow evidence: {root}')
