"""Saved knowledge library actions bind exact host previews without adopting prose."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.generation_sources import canonical_sha
from lg_cli.mcp_approval import operation_details, project_fingerprint
from lg_cli.project_store import ProjectStore
from lg_cli.task_scope import bind_task, check_operation
from lg_cli.workspace_library import entry, library, writing_context
from tests.helpers import make_config


class NarrativeKnowledgeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ,{'LITERARYGIANT_REGISTRY_HOME':str(self.root/'registry')}))
        self.store=ProjectStore(self.root);self.store.initialize(name='Knowledge library actions')
        self.holder=self.store.create_document(kind='character',slug='ada',title='阿达',content='角色身份。',state='accepted')
        self.source=self.store.create_document(kind='chapter',slug='one',title='第一章',content='她误信门后安全。',state='accepted')
        self.store.add_fact(category='world',key='gate',value='门后有危险。',state='canonical')
        self.config=make_config(self.root)

    def propose(self,previous=None,*,content='她相信门后安全。',sources=None,rationale='作者登记角色的信念，不改世界真相。'):
        return self.store.propose_knowledge(slug='gate-belief',title='门后的认知',
            topic={'audience':'character','holder_document_id':self.holder.id,'key':'gate','from_position':3},
            statement={'content':content,'stance':'believes','until_position':20,'sources':sources or []},
            rationale=rationale,expected_version_id=previous['version_id'] if previous else 0)

    def confirm(self,saved):
        return self.store.confirm_knowledge(saved['slug'],proposal_version_number=saved['proposal_version_number'],
            expected_version_id=saved['version_id'])

    def detail(self,saved,topic='pending'):
        return entry(self.config,topic,f"knowledge:{saved['document_id']}")

    def book(self):
        return (self.store.list_documents(limit=None),
            {document.id:self.store.list_versions(document.id,limit=1000) for document in self.store.list_documents(limit=None)},
            self.store.list_facts(limit=None),self.store.list_proposals(limit=1000),project_fingerprint(self.root))

    def prepared(self,saved,action):
        detail=self.detail(saved)
        value=writing_context(self.config,'pending',detail['key'],action,detail['fingerprint'])
        payload=json.loads(value['prompt'].rsplit('\n',1)[1])
        return value,payload

    def assert_compact_preview(self,preview,full,target):
        current=full['knowledge']
        self.assertEqual(set(preview),{'action','topic','current_confirmed_proposal_version_id','target'})
        self.assertEqual(preview['action'],full['action']);self.assertEqual(preview['topic'],current['topic'])
        self.assertEqual(preview['current_confirmed_proposal_version_id'],
            current['confirmed']['proposal_version_id'] if current['confirmed'] is not None else None)
        self.assertEqual(preview['target'],{
            **{key:target[key] for key in ('proposal_version_id','proposal_version_number','stance','until_position','sources')},
            'statement_sha256':canonical_sha(target),'full_content_in_creative_approval':True})
        self.assertNotIn('content',preview['target']);self.assertNotIn('rationale',preview['target'])

    def test_pending_and_bible_read_only_show_proposal_without_generic_adoption(self):
        saved=self.propose();before=self.book()
        with patch('lg_cli.agent_runtime.runtime_factory',side_effect=AssertionError('Library started Runtime')):
            for topic in ('pending','bible'):
                page=library(self.config,topic)
                rows=[row for row in page['entries'] if row['key']==f"knowledge:{saved['document_id']}"]
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['actions'],['confirm-knowledge'])
                detail=self.detail(saved,topic)
                self.assertEqual(detail['status'],'unconfirmed');self.assertIn(saved['proposal']['content'],detail['body'])
                self.assertIn('当前无确认记录',detail['body']);self.assertIn('不晋升 Canonical',detail['body'])
                self.assertIn('不等于 Agent 输入隔离',detail['body'])
                self.assertTrue(detail['versions']);self.assertTrue(all(version['state']=='draft' for version in detail['versions']))
                self.assertTrue(all(not version['key'] for version in detail['versions']))
                for action in ('adopt','select','withdraw','review','generate','withdraw-knowledge'):
                    with self.subTest(topic=topic,action=action),self.assertRaisesRegex(ValueError,'not available'):
                        writing_context(self.config,topic,detail['key'],action,detail['fingerprint'])
        self.assertEqual(self.book(),before)

    def test_confirmation_binds_latest_proposal_number_separately_from_record_head(self):
        first=self.propose();confirmed=self.confirm(first)
        latest=self.propose(confirmed,content='LATEST_PROPOSAL：她开始怀疑门后的安全。')
        withdrawn=self.store.withdraw_knowledge(latest['slug'],expected_version_id=latest['version_id'])
        self.assertNotEqual(withdrawn['version_number'],withdrawn['proposal_version_number'])
        before=self.book();prepared,payload=self.prepared(withdrawn,'confirm-knowledge')
        self.assertEqual(prepared['task'],{'kind':'knowledge','knowledgeAction':'confirm','document':withdrawn['slug'],
            'sourceVersion':withdrawn['version_id'],'versionNumber':latest['proposal_version_number']})
        arguments={'slug':withdrawn['slug'],'expected_version_id':withdrawn['version_id'],
            'proposal_version_number':latest['proposal_version_number']}
        self.assertEqual(payload['arguments'],arguments)
        full=self.store.knowledge_confirmation_preview(**arguments)
        self.assertEqual(full,operation_details(self.root,'studio_knowledge_confirm',arguments)['preview'])
        self.assertEqual(full['proposal_version_id'],latest['proposal_version_id'])
        self.assertEqual(full['proposed_confirmation']['content'],latest['proposal']['content'])
        self.assert_compact_preview(payload['preview'],full,full['proposed_confirmation'])
        self.assertIn('studio_knowledge_confirm',prepared['prompt'])
        scope=bind_task(self.root,prepared['task'])
        self.assertEqual(check_operation(self.root,scope,{'tool':'studio_knowledge_confirm','arguments':arguments}),'grants')
        for change in ({'slug':'one'},{'expected_version_id':latest['version_id']},
                {'proposal_version_number':first['proposal_version_number']},
                {'proposal_version_number':withdrawn['version_number']}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                check_operation(self.root,scope,{'tool':'studio_knowledge_confirm','arguments':{**arguments,**change}})
        for tool in ('studio_knowledge_withdraw','studio_knowledge_propose','studio_accept','studio_adopt_reference','edit_manuscript'):
            with self.subTest(tool=tool),self.assertRaisesRegex(ValueError,'outside the author task'):
                check_operation(self.root,scope,{'tool':tool,'arguments':arguments})
        self.assertEqual(self.book(),before)

    def test_confirmed_record_withdraw_preview_and_scope_preserve_exact_confirmation(self):
        confirmed=self.confirm(self.propose());before=self.book()
        detail=self.detail(confirmed);self.assertEqual(detail['actions'],['withdraw-knowledge'])
        prepared,payload=self.prepared(confirmed,'withdraw-knowledge')
        self.assertEqual(prepared['task'],{'kind':'knowledge','knowledgeAction':'withdraw',
            'document':confirmed['slug'],'sourceVersion':confirmed['version_id']})
        arguments={'slug':confirmed['slug'],'expected_version_id':confirmed['version_id']}
        self.assertEqual(payload['arguments'],arguments)
        full=self.store.knowledge_withdrawal_preview(**arguments)
        self.assertEqual(full,operation_details(self.root,'studio_knowledge_withdraw',arguments)['preview'])
        self.assertIsNone(full['proposed_confirmation'])
        self.assertEqual(full['knowledge']['confirmed'],confirmed['confirmed'])
        self.assert_compact_preview(payload['preview'],full,confirmed['confirmed'])
        scope=bind_task(self.root,prepared['task'])
        self.assertEqual(check_operation(self.root,scope,{'tool':'studio_knowledge_withdraw','arguments':arguments}),'grants')
        scope['grants']=1
        with self.assertRaisesRegex(ValueError,'stopping point'):
            check_operation(self.root,scope,{'tool':'studio_knowledge_withdraw','arguments':arguments})
        self.assertEqual(self.book(),before)

    def test_old_fingerprint_and_change_during_binding_reject_without_record_write(self):
        first=self.propose();old=self.detail(first)
        latest=self.propose(first,content='作者保存的新提案。');before=self.book()
        with self.assertRaisesRegex(ValueError,'changed after preview'):
            writing_context(self.config,'pending',old['key'],'confirm-knowledge',old['fingerprint'])
        self.assertEqual(self.book(),before)
        current=self.detail(latest);original=self.store.knowledge_confirmation_preview
        def change_after_preview(*args,**kwargs):
            preview=original(*args,**kwargs)
            self.store.create_document(kind='note',slug='concurrent-author-note',title='作者补充',content='绑定期间的独立作者写入。')
            return preview
        with patch.object(ProjectStore,'knowledge_confirmation_preview',side_effect=change_after_preview):
            with self.assertRaisesRegex(ValueError,'changed while binding'):
                writing_context(self.config,'pending',current['key'],'confirm-knowledge',current['fingerprint'])
        self.assertEqual(self.store.knowledge_snapshot(first['slug']),latest)
        self.assertEqual(len(self.store.list_versions(first['slug'])),2)

    def test_stale_source_disables_confirmation_but_allows_exact_withdrawal(self):
        sources=[{'document_id':self.source.id,'version_id':self.source.active_version_id}]
        saved=self.propose(sources=sources);confirmed=self.confirm(saved)
        self.store.edit_active_document('one',content='作者改写了来源。',expected_version_id=self.source.active_version_id,reason='author')
        before=self.book();detail=self.detail(confirmed)
        self.assertEqual(detail['actions'],['withdraw-knowledge']);self.assertIn('来源需复核',detail['body'])
        self.assertIn(confirmed['confirmed']['content'],detail['body'],'Author detail preserves the stale record')
        with self.assertRaisesRegex(ValueError,'not available'):
            writing_context(self.config,'pending',detail['key'],'confirm-knowledge',detail['fingerprint'])
        prepared,payload=self.prepared(confirmed,'withdraw-knowledge')
        full=self.store.knowledge_withdrawal_preview(**payload['arguments'])
        self.assertEqual(full['knowledge']['source_status'],'stale')
        self.assert_compact_preview(payload['preview'],full,confirmed['confirmed'])
        self.assertEqual(check_operation(self.root,bind_task(self.root,prepared['task']),
            {'tool':'studio_knowledge_withdraw','arguments':payload['arguments']}),'grants')
        view=self.store.knowledge_view(audience='character',position=4,holder_document='ada')
        self.assertEqual(view['statements'],[]);self.assertEqual(len(view['withheld']),1)
        self.assertNotIn(confirmed['confirmed']['content'],json.dumps(view,ensure_ascii=False))
        self.assertEqual(self.book(),before)

    def test_latest_proposal_source_controls_confirmation_independently_of_old_confirmation(self):
        sources=[{'document_id':self.source.id,'version_id':self.source.active_version_id}]
        confirmed=self.confirm(self.propose(sources=sources))
        self.store.edit_active_document('one',content='作者的新来源。',expected_version_id=self.source.active_version_id,reason='author')
        source=self.store.get_document('one')
        latest=self.propose(confirmed,content='基于新来源的提案。',sources=[{'document_id':source.id,'version_id':source.active_version_id}])
        self.assertEqual(latest['source_status'],'stale');self.assertEqual(latest['proposal_source_status'],'current')
        self.assertEqual(self.detail(latest)['actions'],['confirm-knowledge','withdraw-knowledge'])
        prepared,payload=self.prepared(latest,'confirm-knowledge')
        full=self.store.knowledge_confirmation_preview(**payload['arguments'])
        self.assertEqual(full['proposed_confirmation']['content'],latest['proposal']['content'])
        self.assert_compact_preview(payload['preview'],full,full['proposed_confirmation'])
        self.assertEqual(prepared['task']['versionNumber'],latest['proposal_version_number'])

    def test_maximum_proposal_and_confirmation_remain_readable_with_exact_preview(self):
        sources=[]
        for number in range(32):
            document=self.store.create_document(kind='note',slug='source-'+str(number),title='来源 '+str(number),
                content='已采纳的合成来源。',state='accepted')
            sources.append({'document_id':document.id,'version_id':document.active_version_id})
        previous=None
        for name,character in (('BMP','旧'),('nonBMP','𠮷'),('quote','"'),('newline','\n'),('control','\x01')):
            with self.subTest(characters=name):
                def maximum(prefix,suffix,length):
                    return prefix+character*(length-len(prefix)-len(suffix))+suffix
                rationale=maximum('RATIONALE_START','RATIONALE_END',4000)
                first=self.propose(previous,content=maximum('CONFIRMED_START','CONFIRMED_END',12000),
                    sources=sources,rationale=rationale)
                confirmed=self.confirm(first)
                latest=self.propose(confirmed,content=maximum('PROPOSAL_START','PROPOSAL_END',12000),
                    sources=sources,rationale=rationale)
                before=self.book();detail=self.detail(latest)
                self.assertFalse(detail['truncated'])
                self.assertIn('CONFIRMED_END',detail['body']);self.assertIn('PROPOSAL_END',detail['body'])
                self.assertEqual(detail['actions'],['confirm-knowledge','withdraw-knowledge'])
                for action,tool in (('confirm-knowledge','studio_knowledge_confirm'),
                        ('withdraw-knowledge','studio_knowledge_withdraw')):
                    with self.subTest(action=action):
                        prepared,payload=self.prepared(latest,action)
                        expected={'slug':latest['slug'],'expected_version_id':latest['version_id']}
                        if action=='confirm-knowledge':expected['proposal_version_number']=latest['proposal_version_number']
                        self.assertEqual(payload['arguments'],expected)
                        full=(self.store.knowledge_confirmation_preview(**expected) if action=='confirm-knowledge'
                            else self.store.knowledge_withdrawal_preview(**expected))
                        self.assertEqual(full,operation_details(self.root,tool,expected)['preview'])
                        target=full['proposed_confirmation'] if action=='confirm-knowledge' else full['knowledge']['confirmed']
                        self.assertEqual(len(target['content']),12000);self.assertEqual(len(target['rationale']),4000)
                        self.assertEqual(len(target['sources']),32)
                        self.assert_compact_preview(payload['preview'],full,target)
                        self.assertLessEqual(len(prepared['prompt'].encode('utf-16-le'))//2,30000)
                        self.assertEqual(prepared['task']['sourceVersion'],latest['version_id'])
                        self.assertEqual(check_operation(self.root,bind_task(self.root,prepared['task']),
                            {'tool':tool,'arguments':expected}),'grants')
                        self.assertEqual(self.prepared(latest,action),(prepared,payload))
                self.assertEqual(self.book(),before)
                previous=latest
