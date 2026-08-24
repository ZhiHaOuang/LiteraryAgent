from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lg_cli.project_store import ProjectStore


class ProjectStoreTests(unittest.TestCase):
    def test_versions_use_document_version_numbers_and_candidates_are_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Version Test")
            store.create_document(kind="note", slug="offset-1", title="Offset 1")
            store.create_document(kind="note", slug="offset-2", title="Offset 2")
            document = store.create_document(
                kind="chapter",
                slug="chapter-1",
                title="Chapter 1",
                content="accepted beginning",
                state="accepted",
            )
            second = store.create_version(
                document.id,
                content="candidate two",
                state="candidate",
            )
            third = store.create_version(
                document.id,
                content="candidate three",
                state="candidate",
            )

            self.assertEqual(store.get_version(document.id, 3).id, third.id)
            self.assertEqual(store.get_document(document.id).content, "accepted beginning")

            accepted = store.accept_version(document.id, second.version_number)
            self.assertEqual(accepted.content, "candidate two")
            self.assertEqual(accepted.active_version_number, 2)

    def test_fact_upsert_and_proposal_acceptance_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Bible Test")
            document = store.create_document(
                kind="world",
                slug="moon",
                title="Moon",
                content="The moon is pale.",
                state="accepted",
            )
            version = store.create_version(
                document.id,
                content="The moon is red.",
                state="candidate",
            )

            first = store.set_fact(category="world", key="moon-color", value="pale")
            updated = store.set_fact(category="world", key="moon-color", value="silver")
            self.assertEqual(first.id, updated.id)
            self.assertEqual(updated.value, "silver")
            self.assertEqual(len(store.fact_history(updated.id)), 2)

            proposal = store.propose_fact(
                category="world",
                key="candidate-moon-color",
                value="red",
                source_document=document.id,
                source_version=version.version_number,
            )
            accepted = store.accept_proposal(proposal.id)
            self.assertEqual(accepted.source_version_id, version.id)
            self.assertEqual(store.list_proposals(status="accepted")[0].id, proposal.id)


if __name__ == "__main__":
    unittest.main()
