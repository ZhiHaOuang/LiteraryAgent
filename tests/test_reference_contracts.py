import copy
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from lg_cli.book_analysis import BookAnalysisStore, response_schema, source_spans, resolve_evidence
from lg_cli.project_store import ProjectStore
from lg_cli.reference_contracts import catalog, card_schema, finalize_cards
from tests.helpers import reference_card


class ReferenceContractTests(unittest.TestCase):
    def test_all_five_categories_reject_wrong_fields_and_types(self):
        for category in catalog()['libraries']:
            card = reference_card(category)
            jsonschema.validate(card, card_schema(category))
            card['unexpected'] = 'unstructured fallback'
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(card, card_schema(category))
            del card['unexpected']
            card['portable_core'] = 'wrong type'
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(card, card_schema(category))

    def test_new_write_requires_card_but_legacy_read_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            store=ProjectStore(Path(raw)); store.initialize()
            doc=store.create_document(kind='chapter',slug='first',title='First',content='The gate closes at dusk.')
            records=BookAnalysisStore(store)
            snapshot=records.snapshot('first')
            span=next(iter(source_spans(snapshot)))
            response={'summary':'Gate rule','entries':[{'key':'gate','label':'Gate','analysis':'Closes at dusk',
                'evidence':[{'span_id':span}]}],'findings':[]}
            with self.assertRaises(jsonschema.ValidationError):
                records.save(snapshot,'Worldview',resolve_evidence(snapshot,response),'bad')
            self.assertFalse(records.root.exists())
            response['entries'][0]['instance_card']=reference_card('Worldview')
            response['entries'][0]['instance_card']['rule_application']['rule_statement']='Closes at dusk'
            jsonschema.validate(response,response_schema('Worldview'))
            report=resolve_evidence(snapshot,finalize_cards(response,'Worldview',source_spans(snapshot)))
            payload=records.save(snapshot,'Worldview',report,'good')
            path=records.root/'first'/'Worldview.json'
            del payload['contract']
            del payload['report']['entries'][0]['instance_card']
            path.write_text(json.dumps(payload))
            before=path.read_bytes()
            self.assertEqual(records.list()[0]['status'],'current')
            self.assertEqual(path.read_bytes(),before)

    def test_worldview_examples_cannot_reference_foreign_source(self):
        card=reference_card('Worldview')
        card['rule_application']['event_examples']=[{'chunk_id':'external','application':'rule'}]
        with self.assertRaises(ValueError):
            finalize_cards({'entries':[{'instance_card':card,'evidence':[{'span_id':'local'}]}]},'Worldview',{'local':{}})
