"""Synthetic coverage of the shared wikilink resolver's inbound graph."""

import copy
import unittest

from memory_dream import audit


def record(stem, name="", body=""):
    return {"stem": stem, "name": name, "body": body}


class InboundIndexTests(unittest.TestCase):
    def test_unique_links_aliases_prefixes_and_multiline_targets(self):
        records = {
            "source.md": record("source", body=(
                "[[target]] [[Target Name]] [[target]] [[feedback]] "
                "[[project]] [[reference]] [[multi\n line]] [[missing]]"
            )),
            "nested/target.md": record("target", "Target Name"),
            "feedback.md": record("feedback_feedback"),
            "project.md": record("project_project"),
            "reference.md": record("reference_reference"),
            "multi.md": record("multiline"),
            "unlinked.md": record("unlinked"),
        }
        original = copy.deepcopy(records)
        self.assertEqual(audit.inbound_index(records), {
            "source.md": set(), "nested/target.md": {"source.md"},
            "feedback.md": {"source.md"}, "project.md": {"source.md"},
            "reference.md": {"source.md"}, "multi.md": {"source.md"},
            "unlinked.md": set(),
        })
        self.assertEqual(records, original)

    def test_ambiguous_canonical_tokens_and_duplicate_target_owners(self):
        records = {
            "source.md": record("source", body="[[alpha beta]] [[dup]] [[Shared]] [[good]]"),
            "one.md": record("alpha-beta", "Shared"),
            "two.md": record("alpha_beta", "Shared"),
            "a/dup.md": record("dup"),
            "b/dup.md": record("dup"),
            "good.md": record("good", "good"),
        }
        expected = {path: set() for path in records}
        expected["good.md"] = {"source.md"}
        self.assertEqual(audit.inbound_index(records), expected)

    def test_self_links_are_omitted_and_distinct_sources_are_retained(self):
        records = {
            "target.md": record("target", "Friendly", "[[target]] [[Friendly]]"),
            "first.md": record("first", body="[[target]] [[Friendly]] [[target]]"),
            "second.md": record("second", body="[[target]]"),
        }
        self.assertEqual(audit.inbound_index(records), {
            "target.md": {"first.md", "second.md"},
            "first.md": set(), "second.md": set(),
        })
        self.assertEqual(audit.inbound_index({}), {})


if __name__ == "__main__":
    unittest.main()
