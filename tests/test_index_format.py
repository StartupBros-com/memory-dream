#!/usr/bin/env python3
"""Deterministic regressions for append-only index style inference.

All notes, findings and approval transcripts below are synthetic fixtures in
temporary directories. CLI children use the existing isolated environment
helper, and no provider, model, network or personal-memory access is needed.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for directory in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from memory_dream import assemble, audit, config  # noqa: E402
from test_apply import Harness  # noqa: E402
from test_assemble import run_cli, subprocess_env, write_findings  # noqa: E402


def note(name: str, description: str, body: str = "Synthetic durable fact.") -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  type: project\n---\n{body}\n"
    )


def style_line(stem: str, style: str = "title-dash", hook: str = "Short hook") -> str:
    label = stem
    if style.startswith("title-"):
        label = stem.replace("-", " ").replace("_", " ")
        label = label[:1].upper() + label[1:]
    separator = " — " if style.endswith("-dash") else ": "
    return f"- [{label}]({stem}.md){separator}{hook}\n"


def examples(style: str = "title-dash", count: int = 3, hook: str = "Short hook") -> str:
    return "".join(style_line(f"topic-{i:03d}", style, hook) for i in range(count))


def append_note(index: str, description: str, name: str = "new-note") -> str:
    # The in-memory content path is also what build uses for not-yet-written
    # results. A missing on-disk note must not force a canonical append.
    with tempfile.TemporaryDirectory() as temporary:
        return audit.render_index_reconcile(
            index,
            Path(temporary),
            {"dropped": [], "appended": ["new-note.md"], "fragment_dropped": []},
            {"new-note.md": note(name, description, "# Unrelated display heading\nFact.")},
        )


class DetectIndexFormatTests(unittest.TestCase):
    def assert_format(self, index, selected, recognized, unrecognized=0, mixed=False, budget=None):
        result = audit.detect_index_format(index)
        self.assertEqual(result, {
            "format": selected,
            "mixed": mixed,
            "recognized": recognized,
            "unrecognized": unrecognized,
            "hook_bytes": budget,
        })
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertIs(type(result["mixed"]), bool)
        self.assertIs(type(result["recognized"]), int)
        self.assertIs(type(result["unrecognized"]), int)
        return result

    def test_detects_all_four_simple_styles(self):
        for style in ("canonical", "title-colon", "title-dash", "name-dash"):
            with self.subTest(style=style):
                self.assert_format(
                    examples(style), style, 3,
                    budget=None if style == "canonical" else len(b"Short hook"),
                )

    def test_109_line_title_dash_reproduction(self):
        self.assert_format(examples(count=109), "title-dash", 109, budget=10)

    def test_exact_eighty_percent_support_is_inclusive(self):
        # A long minority hook must not inflate the dominant style's budget.
        index = examples(count=4) + style_line("minority-note", "canonical", "x" * 200)
        self.assert_format(index, "title-dash", 5, mixed=True, budget=10)

    def test_just_below_confidence_threshold_falls_back(self):
        index = examples(count=3) + style_line("minority-note", "canonical")
        self.assert_format(index, None, 4, mixed=True)

    def test_tied_styles_fall_back(self):
        index = examples(count=3) + "".join(
            style_line(f"other-{i}", "title-colon") for i in range(3)
        )
        self.assert_format(index, None, 6, mixed=True)

    def test_insufficient_distinct_examples_fall_back(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.assert_format(examples(count=count), None, count)

    def test_empty_and_non_index_text_do_not_vote(self):
        for index in ("", "# Memory\n\nNotes live here.\n", "- Ordinary prose without a local link.\n"):
            with self.subTest(index=index):
                self.assert_format(index, None, 0)

    def test_prose_headings_external_links_and_fences_do_not_vote(self):
        ignored = (
            "# [Navigation](heading.md)\n"
            "Read [the guide](guide.md) when needed.\n"
            "- [External](https://example.invalid/guide.md) — unrelated\n"
            "```markdown\n" + examples("canonical", count=4) + "```\n"
            "~~~\n" + examples("name-dash", count=4) + "~~~\n"
        )
        self.assert_format(ignored + examples(), "title-dash", 3, budget=10)

    def test_protocol_relative_and_parent_segments_in_external_urls_do_not_vote(self):
        for target in ("//example.com/docs/example.md", "https://example.com/../docs/example.md"):
            with self.subTest(target=target):
                external = f"- [External]({target}) — Not a local note\n"
                self.assert_format(external + examples(), "title-dash", 3, budget=10)

    def test_all_occurrences_of_duplicated_targets_are_unrecognized(self):
        repeated = style_line("duplicate-note")
        index = examples() + repeated + repeated
        self.assert_format(index, None, 3, unrecognized=2, mixed=True)
        self.assert_format(repeated * 10, None, 0, unrecognized=10, mixed=True)

    def test_duplicate_target_also_present_on_packed_line_is_not_evidence(self):
        index = examples() + "- [Topic 000](topic-000.md); [Extra](extra-note.md)\n"
        self.assert_format(index, None, 2, unrecognized=2, mixed=True)

    def test_packed_and_unsupported_local_lines_count_once_per_line(self):
        for unsupported in (
            "- [Packed one](packed-one.md); [Packed two](packed-two.md)\n",
            "- [Repeated](packed-one.md) and [Repeated](packed-one.md)\n",
            "1. [Numbered entry](numbered-entry.md): unsupported\n",
            "- [Bare entry](bare-entry.md)\n",
            "- [Decorated entry](decorated-entry.md) | unsupported\n",
        ):
            with self.subTest(unsupported=unsupported):
                self.assert_format(examples() + unsupported, None, 3, unrecognized=1, mixed=True)
                self.assert_format(
                    examples(count=4) + unsupported,
                    "title-dash", 4, unrecognized=1, mixed=True, budget=10,
                )

    def test_observed_budget_is_maximum_utf8_bytes(self):
        hooks = ("abc", "ééééé", "seven77")
        index = "".join(style_line(f"topic-{i}", hook=hook) for i, hook in enumerate(hooks))
        self.assert_format(index, "title-dash", 3, budget=10)


class RenderIndexFormatTests(unittest.TestCase):
    def test_109_existing_lines_are_unchanged_and_new_entry_matches(self):
        index = examples(count=109, hook="Routing hook here.")
        description = "Caches expire after deployments to keep assets consistent across regions."
        rendered = append_note(index, description, name="cache-entry")
        self.assertEqual(rendered, index + "- [Cache entry](new-note.md) — Caches expire…\n")
        self.assertEqual(rendered.splitlines()[:109], index.splitlines())

    def test_title_colon_and_name_dash_append_styles(self):
        for style, expected in (
            ("title-colon", "- [New note](new-note.md): Alpha beta…\n"),
            ("name-dash", "- [new-note](new-note.md) — Alpha beta…\n"),
        ):
            with self.subTest(style=style):
                index = examples(style, hook="x" * 13)
                self.assertEqual(append_note(index, "Alpha beta gamma delta"), index + expected)

    def test_title_comes_only_from_frontmatter_name_and_preserves_acronyms(self):
        index = examples(hook="x" * 50)
        self.assertEqual(
            append_note(index, "A full description fits.", name="api_ID-note"),
            index + "- [Api ID note](new-note.md) — A full description fits.\n",
        )

    def test_separator_only_name_does_not_become_an_empty_link_label(self):
        for style, separator in (("title-dash", " — "), ("title-colon", ": ")):
            with self.subTest(style=style):
                index = examples(style, hook="x" * 50)
                self.assertEqual(
                    append_note(index, "A full description fits.", name="-_"),
                    index + f"- [-_](new-note.md){separator}A full description fits.\n",
                )

    def test_canonical_empty_ambiguous_and_insufficient_stores_keep_old_append(self):
        description = "A complete description remains verbatim even when older hooks are short."
        canonical = audit.index_entry_line("new-note", description, "new-note.md") + "\n"
        indexes = (
            "",
            examples("canonical", hook="x"),
            examples(count=2, hook="x"),
            examples() + style_line("minority", "canonical"),
        )
        for index in indexes:
            with self.subTest(index=index):
                self.assertEqual(append_note(index, description), index + canonical)

    def test_utf8_word_boundaries_and_full_description_exact_fit(self):
        description = "Café noir encore"
        for budget, expected in ((13, "Café noir…"), (12, "Café…"), (17, description)):
            with self.subTest(budget=budget):
                index = examples(hook="x" * budget)
                rendered = append_note(index, description)
                self.assertEqual(rendered, index + f"- [New note](new-note.md) — {expected}\n")
                self.assertLessEqual(len(expected.encode("utf-8")), budget)
                if expected.endswith("…"):
                    self.assertTrue(description.startswith(expected[:-1]))

    def test_internal_whitespace_is_preserved_in_literal_prefix(self):
        index = examples(hook="x" * 14)
        self.assertEqual(
            append_note(index, "alpha  beta gamma delta"),
            index + "- [New note](new-note.md) — alpha  beta…\n",
        )

    def test_no_complete_first_word_plus_ellipsis_keeps_full_description(self):
        for description, budget in (
            ("Extraordinary details remain intact", 12),
            ("Café noir encore", 7),
            ("Word after another word", 3),
        ):
            with self.subTest(description=description, budget=budget):
                index = examples(hook="x" * budget)
                self.assertEqual(
                    append_note(index, description),
                    index + f"- [New note](new-note.md) — {description}\n",
                )

    def test_existing_custom_lines_are_preserved_byte_for_byte(self):
        index = examples(count=4) + "- [Hand-edited](custom-note.md) | preserve  spacing!  \n"
        self.assertEqual(
            append_note(index, "Alpha beta gamma delta"),
            index + "- [New note](new-note.md) — Alpha…\n",
        )

    def test_style_evidence_is_taken_before_reconcile_drops_old_entries(self):
        index = examples()
        with tempfile.TemporaryDirectory() as temporary:
            rendered = audit.render_index_reconcile(
                index, Path(temporary),
                {"dropped": ["topic-000.md", "topic-001.md"], "appended": ["new-note.md"]},
                {"new-note.md": note("new-note", "Alpha beta gamma delta")},
            )
        self.assertEqual(rendered, style_line("topic-002") + "- [New note](new-note.md) — Alpha…\n")

    def test_empty_description_keeps_existing_canonical_append_behavior(self):
        index = examples()
        self.assertEqual(append_note(index, ""), index + "- [new-note](new-note.md)\n")

    def test_disk_frontmatter_and_in_memory_preview_render_identically(self):
        index = examples(hook="x" * 13)
        content = note("new-note", "Alpha beta gamma delta")
        reconcile = {"dropped": [], "appended": ["new-note.md"], "fragment_dropped": []}
        with tempfile.TemporaryDirectory() as temporary:
            memory = Path(temporary)
            (memory / "new-note.md").write_text(content, encoding="utf-8", newline="\n")
            from_disk = audit.render_index_reconcile(index, memory, reconcile)
            from_draft = audit.render_index_reconcile(index, memory, reconcile, {"new-note.md": content})
        self.assertEqual(from_disk, from_draft)
        self.assertEqual(from_disk, index + "- [New note](new-note.md) — Alpha beta…\n")

    def test_reconcile_is_idempotent_after_appending(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = Path(temporary)
            index = examples()
            for i in range(3):
                stem = f"topic-{i:03d}"
                (memory / f"{stem}.md").write_text(note(stem, "Old full description stays unchanged."), encoding="utf-8")
            (memory / "new-note.md").write_text(note("new-note", "Alpha beta gamma delta"), encoding="utf-8")
            index_path = memory / "MEMORY.md"
            index_path.write_text(index, encoding="utf-8", newline="\n")
            records = audit.scan_project_notes(memory)
            reconcile = audit.compute_index_reconcile("proj", memory, records)
            self.assertEqual(reconcile["appended"], ["new-note.md"])
            rendered = audit.render_index_reconcile(index, memory, reconcile)
            self.assertEqual(rendered, index + "- [New note](new-note.md) — Alpha…\n")
            index_path.write_text(rendered, encoding="utf-8", newline="\n")
            self.assertIsNone(audit.compute_index_reconcile("proj", memory, audit.scan_project_notes(memory)))

    def test_canonical_renderer_and_refresh_contract_stay_unchanged(self):
        self.assertEqual(audit.index_entry_line("raw-name", "full hook", "raw-name.md"), "- [raw-name](raw-name.md): full hook")
        self.assertEqual(audit.index_entry_line("raw-name", "", "raw-name.md"), "- [raw-name](raw-name.md)")
        canonical = "- [raw-name](raw-name.md): old full description\n"
        custom = "- [Custom note](custom-note.md) — short hook\n"
        packed = "- [raw-name](raw-name.md); [Custom note](custom-note.md)\n"
        refresh = {
            "raw-name.md": {"old": ("raw-name", "old full description"), "new": ("raw-name", "new full description"), "force": False},
            "custom-note.md": {"old": ("custom-note", "old full description"), "new": ("custom-note", "new full description"), "force": False},
        }
        self.assertEqual(
            audit.refresh_index_lines(canonical + custom + packed, refresh),
            "- [raw-name](raw-name.md): new full description\n" + custom + packed,
        )
        refresh["custom-note.md"]["force"] = True
        self.assertEqual(
            audit.refresh_index_lines(custom + packed, refresh),
            "- [custom-note](custom-note.md): new full description\n" + packed,
        )


class IndexFormatBuildApplyTests(unittest.TestCase):
    HOOK = "Routing hook here."
    NEW_DESCRIPTION = "Caches expire after deployments to keep assets consistent across regions."

    def fixture(self, root, count=109, style="title-dash"):
        harness = Harness(root)
        live = harness.project(mirror=False)
        stems = ["source-note"] + [f"topic-{i:03d}" for i in range(count - 1)]
        for stem in stems:
            (live / f"{stem}.md").write_text(
                note(stem, "Original source for the durable deployment checklist"),
                encoding="utf-8", newline="\n",
            )
        index = "".join(style_line(stem, style, self.HOOK) for stem in stems)
        (live / "MEMORY.md").write_text(index, encoding="utf-8", newline="\n")
        cluster = {
            "cluster_id": "synthetic-style-cluster", "project": "proj", "top_score": 5,
            "notes": [{"path": "source-note.md", "name": "source-note", "body": "Synthetic durable fact.", "score": 5, "supersessions": 0}],
        }
        draft = {
            "action": "split", "justification": "Separate the independent cache fact from the deployment checklist",
            "survivor": {
                "path": "source-note.md",
                "content": note("source-note", "Core deployment checklist retained for future use", "Retain the checklist. See [[cache-entry]] for cache expiry."),
            },
            "extracts": [{"path": "cache-entry.md", "content": note("cache-entry", self.NEW_DESCRIPTION)}],
            "deletes": [],
        }
        (root / "plan.json").write_text(json.dumps({"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}), encoding="utf-8")
        (root / "drafts.json").write_text(json.dumps({"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [draft]}]}), encoding="utf-8")
        write_findings(root)
        return harness, live, index

    def build(self, harness, *extra, out=None):
        return run_cli(
            "build", "--live-root", str(harness.live_root),
            "--plan", str(harness.root / "plan.json"), "--drafts", str(harness.root / "drafts.json"),
            "--findings", str(harness.root / "findings.json"),
            "--out", str(out or harness.patch_set), "--created-at-line", "2", "--stamp", "2026-07-31",
            *extra, env=subprocess_env(harness.claude_config_dir),
        )

    def approve_built_fixture(self, harness, manifest):
        # Deliberately do not use Harness.write(): the real build manifest must
        # remain untouched so this exercises the actual content-bound token.
        harness.manifest_id = manifest["id"]
        harness.approval = f"approve all {harness.manifest_id}"
        harness.transcript = harness.live_root / "proj" / "synthetic-session.jsonl"
        turns = [
            {"type": "user", "message": {"role": "user", "content": "Review the synthetic fixture"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Preview generated"}},
            {"type": "user", "message": {"role": "user", "content": harness.approval}},
        ]
        harness.transcript.write_text("\n".join(json.dumps(turn) for turn in turns) + "\n", encoding="utf-8")
        return harness.selection([proposal["id"] for proposal in manifest["proposals"]], trace={
            "message_sha256": hashlib.sha256(harness.approval.encode("utf-8")).hexdigest(),
            "turn_index": 2,
        })

    def test_actual_build_preview_matches_trace_approved_apply(self):
        for style in ("title-dash", "title-colon", "name-dash", "canonical"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as temporary:
                harness, live, original = self.fixture(Path(temporary), count=109, style=style)
                built = self.build(harness)
                self.assertEqual(built.returncode, 0, built.stderr)
                manifest_bytes = (harness.patch_set / "manifest.json").read_bytes()
                manifest = json.loads(manifest_bytes)
                self.assertEqual(len(manifest["proposals"]), 1)
                report = json.loads((harness.patch_set / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["dropped"], [])
                self.assertEqual(report["index_format_drift"], [])
                label = "Cache entry" if style.startswith("title-") else "cache-entry"
                separator = " — " if style.endswith("-dash") else ": "
                hook = self.NEW_DESCRIPTION if style == "canonical" else "Caches expire…"
                expected = original + f"- [{label}](cache-entry.md){separator}{hook}\n"
                expected_diff = "".join(difflib.unified_diff(
                    original.splitlines(keepends=True), expected.splitlines(keepends=True),
                    fromfile="a/MEMORY.md", tofile="b/MEMORY.md",
                ))
                self.assertEqual((harness.patch_set / "index-proj.diff").read_text(encoding="utf-8"), expected_diff)
                self.assertEqual(assemble.anticipated_index_texts(live, manifest["proposals"]), (original, expected))
                self.assertEqual((live / "MEMORY.md").read_text(encoding="utf-8"), original)
                self.assertFalse((live / "cache-entry.md").exists())
                selection = self.approve_built_fixture(harness, manifest)
                applied = run_cli(
                    "apply", "--patch-set", str(harness.patch_set), "--selection", str(selection),
                    "--transcript", str(harness.transcript), "--live-root", str(harness.live_root),
                    env=subprocess_env(harness.claude_config_dir),
                )
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertIn("DREAM-APPLY-COMPLETE", applied.stdout)
                self.assertEqual((live / "MEMORY.md").read_bytes(), expected.encode("utf-8"))
                self.assertEqual((harness.patch_set / "manifest.json").read_bytes(), manifest_bytes)
                for proposal in manifest["proposals"]:
                    for result in proposal["results"]:
                        self.assertEqual((live / result["path"]).read_text(encoding="utf-8"), result["content"])
                self.assertIsNone(audit.compute_index_reconcile("proj", live, audit.scan_project_notes(live)))

    def test_build_reports_mixed_and_unsupported_counts_without_hook_text(self):
        for packed in (False, True):
            with self.subTest(packed=packed), tempfile.TemporaryDirectory() as temporary:
                harness, live, _original = self.fixture(Path(temporary), count=5)
                sentinel = "synthetic-private-hook-marker"
                lines = [style_line("source-note", hook=sentinel)]
                lines += [style_line(f"topic-{i:03d}", hook=sentinel) for i in range(2 if packed else 3)]
                if packed:
                    lines.append(f"- [Packed](topic-002.md); [Pair](topic-003.md) — {sentinel}\n")
                else:
                    lines.append(style_line("topic-003", "canonical", sentinel))
                (live / "MEMORY.md").write_text("".join(lines), encoding="utf-8", newline="\n")
                built = self.build(harness)
                self.assertEqual(built.returncode, 0, built.stderr)
                report = json.loads((harness.patch_set / "report.json").read_text(encoding="utf-8"))
                drift = report["index_format_drift"]
                self.assertEqual(len(drift), 1)
                self.assertEqual(drift[0]["project"], "proj")
                self.assertEqual(drift[0]["recognized"], 3 if packed else 5)
                self.assertEqual(drift[0]["unrecognized"], 1 if packed else 0)
                self.assertNotIn(sentinel, json.dumps(drift))
                self.assertNotIn(sentinel, built.stderr)
                self.assertIn("WARN", built.stderr)
                self.assertRegex(built.stderr.lower(), r"index[ -]format")

    def test_style_preservation_does_not_bypass_consent_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness, live, original = self.fixture(Path(temporary), count=3)
            built = self.build(harness)
            self.assertEqual(built.returncode, 0, built.stderr)
            manifest = json.loads((harness.patch_set / "manifest.json").read_text(encoding="utf-8"))
            selection = self.approve_built_fixture(harness, manifest)
            payload = json.loads(selection.read_text(encoding="utf-8"))
            payload["operator_trace"] = {}
            selection.write_text(json.dumps(payload), encoding="utf-8")
            refused = run_cli(
                "apply", "--patch-set", str(harness.patch_set), "--selection", str(selection),
                "--transcript", str(harness.transcript), "--live-root", str(harness.live_root),
                env=subprocess_env(harness.claude_config_dir),
            )
            self.assertEqual(refused.returncode, 1, refused.stderr)
            self.assertIn("operator trace invalid", refused.stderr)
            self.assertEqual((live / "MEMORY.md").read_text(encoding="utf-8"), original)
            self.assertFalse((live / "cache-entry.md").exists())

    def test_index_growth_cap_refusal_is_still_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness, live, original = self.fixture(Path(temporary), count=config.INDEX_LOAD_MAX_LINES + 1)
            refused = self.build(harness)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("GROWS the over-cap index", refused.stderr)
            self.assertFalse((harness.patch_set / "manifest.json").exists())
            self.assertEqual((live / "MEMORY.md").read_text(encoding="utf-8"), original)
            allowed_out = harness.root / "explicitly-allowed"
            allowed = self.build(harness, "--allow-index-growth", out=allowed_out)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            report = json.loads((allowed_out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["index_over_cap"][0]["project"], "proj")
            self.assertGreater(report["index_over_cap"][0]["lines"], config.INDEX_LOAD_MAX_LINES)


if __name__ == "__main__":
    unittest.main()
