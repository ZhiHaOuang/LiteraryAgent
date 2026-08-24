from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.knowledge import KnowledgeGateway, KnowledgeTier

from tests.helpers import make_config


class KnowledgeGatewayTests(unittest.TestCase):
    def test_searches_abstract_then_bridge_and_requires_raw_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "Library"
            abstract = library / "AbstractLibrary"
            bridge = library / "BridgeIndex" / "books"
            raw_root = library / "TaciturnRaw"
            abstract.mkdir(parents=True)
            bridge.mkdir(parents=True)
            raw_root.mkdir(parents=True)
            (abstract / "pattern_index.jsonl").write_text(
                json.dumps({"pattern_id": "abstract-1", "summary": "dragon succession conflict"}) + "\n",
                encoding="utf-8",
            )
            (bridge / "bridge.jsonl").write_text(
                json.dumps({"book_id": "bridge-1", "summary": "dragon alliance reversal"}) + "\n",
                encoding="utf-8",
            )
            (raw_root / "raw.json").write_text(
                json.dumps({"id": "raw-1", "summary": "dragon source prose"}),
                encoding="utf-8",
            )
            config = make_config(root, library_path=library)
            gateway = KnowledgeGateway(config)

            regular = gateway.search("dragon", top_k=2)
            self.assertEqual([hit.tier for hit in regular.hits], [KnowledgeTier.ABSTRACT, KnowledgeTier.BRIDGE])
            self.assertNotIn(KnowledgeTier.RAW.value, regular.tiers_searched)
            self.assertTrue(any("disabled" in warning for warning in regular.warnings))

            raw = gateway.search("dragon", top_k=3, allow_raw=True)
            self.assertIn(KnowledgeTier.RAW.value, raw.tiers_searched)
            self.assertEqual(raw.hits[-1].tier, KnowledgeTier.RAW)
            self.assertIn("untrusted evidence", raw.to_prompt_text())


if __name__ == "__main__":
    unittest.main()
