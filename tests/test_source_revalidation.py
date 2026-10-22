import asyncio
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.generation_sources import bind_generation_sources, make_observation, make_receipt
from lg_cli.mcp_approval import project_fingerprint
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config


class SourceRevalidationTests(unittest.TestCase):
    def test_old_source_reports_exact_candidate_action_without_revoking_author_fact(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();store=ProjectStore(root);store.initialize()
            source=store.create_document(kind='note',slug='source',title='Source',content='Before',state='accepted')
            chapter=store.create_document(kind='chapter',slug='one',title='One',content='Author manuscript',state='accepted')
            fact=store.set_fact(category='world',key='gate',value='Gate closes',source_document='source',source_version=1)
            receipt=make_receipt(mode='live',run_id='source-review-run',observations=[make_observation('read_document',asdict(source))])
            with bind_generation_sources(receipt):
                candidate=store.create_version('one',content='Candidate',expected_version_id=chapter.active_version_id)
            store.edit_active_document('source',content='Changed',expected_version_id=source.active_version_id,reason='author')
            before=project_fingerprint(root)
            async def execute():
                backend=Backend(make_config(root))
                try:
                    with patch('lg_cli.backend.runtime_factory',side_effect=AssertionError('No runtime for source review')):
                        value=await backend.dispatch('sources/check',{'document':'source','version':1})
                    self.assertEqual(value['coverage'],'known_direct_associations_only')
                    self.assertFalse(value['automaticChanges'])
                    self.assertEqual(value['reviewCandidates'],[{'document':'one','version':candidate.version_number,
                        'title':'One','action':'review_then_author_adoption'}])
                    self.assertEqual(value['impact']['coverage'],'partial')
                    self.assertTrue(value['impact']['unknown_reasons'])
                finally: await backend.close()
            asyncio.run(execute())
            self.assertEqual(store.get_fact(fact.id).state,'canonical')
            self.assertEqual(store.get_document('one').active_version_id,chapter.active_version_id)
            self.assertEqual(project_fingerprint(root),before)
