from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.knowledge import KnowledgeGateway, KnowledgeTier

from tests.helpers import make_config


class KnowledgeGatewayTests(unittest.TestCase):
    def test_pattern_instances_keep_distinct_source_ids(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "Library"
            abstract = library / "AbstractLibrary"
            abstract.mkdir(parents=True)
            (abstract / "pattern_index.jsonl").write_text(json.dumps({
                "pattern_id": "pattern", "library": "CharacterArc", "summary": "sacrifice"
            }), encoding="utf-8")
            (abstract / "instance_index.jsonl").write_text("\n".join(json.dumps({
                "pattern_id": "pattern", "instance_id": key, "library": "CharacterArc", "summary": "sacrifice"
            }) for key in ("instance-one", "instance-two")), encoding="utf-8")
            result = KnowledgeGateway(make_config(root, library_path=library)).search("sacrifice", top_k=10)
            self.assertEqual({hit.source_id for hit in result.hits}, {"pattern", "instance-one", "instance-two"})

    def test_library_filter_preserves_category_and_excludes_unclassified_hits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "Library"
            abstract = library / "AbstractLibrary"
            abstract.mkdir(parents=True)
            records = [
                {"pattern_id": "arc", "library": "CharacterArc", "summary": "succession"},
                {"pattern_id": "event", "library": "EventsLibrary", "summary": "succession"},
                {"pattern_id": "unknown", "summary": "succession"},
            ]
            (abstract / "pattern_index.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            gateway = KnowledgeGateway(make_config(root, library_path=library))
            self.assertEqual(len(gateway.search("succession", top_k=10).hits), 3)
            context = gateway.search("succession", library="CharacterArc")
            self.assertEqual([hit.source_id for hit in context.hits], ["arc"])
            self.assertEqual(context.to_metadata()["hits"][0]["library"], "CharacterArc")
            self.assertIn("library: CharacterArc", context.to_prompt_text())
            self.assertEqual(gateway.search("succession", library="Worldview").hits, ())

    def test_source_locked_details_are_not_searchable_or_prompted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "Library"
            abstract = library / "AbstractLibrary"
            abstract.mkdir(parents=True)
            records = [
                {
                    "instance_id": "nested",
                    "instance_card": {
                        "mechanism": "succession conflict",
                        "source_locked_details": {"name": "lockedsecret"},
                    },
                },
                {
                    "instance_id": "summary",
                    "summary": {
                        "mechanism": "succession conflict",
                        "source_locked_details": ["lockedsecret"],
                    },
                },
            ]
            index = abstract / "instance_index.jsonl"
            original = "\n".join(json.dumps(record) for record in records) + "\n"
            index.write_text(original, encoding="utf-8")
            gateway = KnowledgeGateway(make_config(root, library_path=library))

            self.assertEqual(gateway.search("lockedsecret").hits, ())
            context = gateway.search("succession")
            self.assertEqual(len(context.hits), 2)
            self.assertNotIn("lockedsecret", context.to_prompt_text())
            self.assertIn("succession conflict", context.to_prompt_text())
            self.assertEqual(index.read_text(encoding="utf-8"), original)

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
