"""New split notes shed identity fields; survivors and provenance stay intact."""

import json
import tempfile
import unittest
from pathlib import Path

import test_assemble as fixtures
from memory_dream import assemble, audit, config


class ExtractIdentityTests(unittest.TestCase):
    _project = fixtures.AssembleTests._project
    _cluster = fixtures.AssembleTests._cluster
    _split_draft = fixtures.SplitRedescribeTests._split_draft

    STAMP = "2026-07-31"
    HEADER = "---\nname: mega\ndescription: old hook that no longer routes\n"
    IDENTITY = "originSessionId: sess-source\nmodified: 2026-01-02T03:04:05Z\n"

    def _draft(self):
        draft = self._split_draft()
        for extract in draft["extracts"]:
            # A drafter can also provide stale identity in either layout.
            content = extract["content"].replace(
                "metadata:\n",
                "originSessionId: sess-drafter-flat\nmodified: yesterday\n"
                "metadata:\n  originSessionId: sess-drafter-nested\n"
                "  modified: yesterday\n",
            )
            extract["content"] = content + (
                "\nSource: [[mega]]\n\n```yaml\n"
                "originSessionId: body-example\n  modified: body-example\n```\n"
            )
        return draft

    def _assert_no_identity(self, content):
        metadata, error, _body, _raw = audit.parse_frontmatter(content)
        self.assertIsNone(error)
        for fields in (metadata, metadata.get("metadata", {})):
            self.assertNotIn("originSessionId", fields)
            self.assertNotIn("modified", fields)

    def test_new_extract_identity_removed_across_supported_layouts(self):
        schema = "node_type: memory\ntype: project\n" + self.IDENTITY
        nested = "metadata:\n" + "".join("  " + line for line in schema.splitlines(True))
        donor_shapes = {
            "nested": nested,
            "legacy flat": schema,
            "mixed": self.IDENTITY + nested,
            "nested with decay": nested + (
                "  confidence: 0.9\n  maturity: stable\n"
                "  last_validated: 2026-01-01\n"
            ),
        }
        for label, schema_fields in donor_shapes.items():
            for newline in ("\n", "\r\n"):
                with self.subTest(layout=label, newline=repr(newline)), tempfile.TemporaryDirectory() as temp:
                    # Unknown schema fields and plural provenance are not identity.
                    donor = (self.HEADER + schema_fields + (
                        "custom_field: kept\noriginSessionIds: sess-a, sess-b\n"
                        "custom:\n  modified: keep-custom-value\n"
                        "  originSessionId: keep-custom-session\n---\nSource prose.\n"
                    )).replace("\n", newline)
                    live_root, live = self._project(Path(temp), {"mega.md": donor})
                    cluster = self._cluster(["mega.md"], live)
                    draft = self._draft()
                    drafts = {cluster["cluster_id"]: [draft]}
                    proposals, dropped = assemble.assemble_proposals(
                        [cluster], drafts, live_root, self.STAMP
                    )
                    self.assertEqual(dropped, [])
                    self.assertEqual(len(proposals), 1)
                    proposal = proposals[0]
                    self.assertEqual(proposal["sources"], [
                        {"path": "mega.md", "digest": audit.digest(live / "mega.md")}
                    ])
                    self.assertEqual(proposal["deletes"], [])
                    # Existing survivor is precisely the old preservation path.
                    extra = {"last_validated": self.STAMP} if "decay" in label else None
                    self.assertEqual(proposal["results"][0]["content"], audit.preserve_metadata(
                        draft["survivor"]["content"], donor, extra
                    ))
                    self.assertEqual(audit.origin_session_id(proposal["results"][0]["content"]), "sess-source")
                    for result, extract in zip(proposal["results"][1:], draft["extracts"]):
                        content = result["content"]
                        with self.subTest(path=result["path"]):
                            self._assert_no_identity(content)
                            metadata = audit.parse_frontmatter(content)[0]
                            self.assertEqual(metadata["metadata"]["node_type"], "memory")
                            self.assertEqual(metadata["metadata"]["type"],
                                             audit.parse_frontmatter(extract["content"])[0]["metadata"]["type"])
                            self.assertEqual(metadata["metadata"]["confidence"], config.NEW_EXTRACT_CONFIDENCE)
                            self.assertEqual(metadata["metadata"]["maturity"], config.NEW_EXTRACT_MATURITY)
                            self.assertEqual(metadata["metadata"]["last_validated"], self.STAMP)
                            self.assertEqual(metadata["custom_field"], "kept")
                            self.assertEqual(metadata["originSessionIds"], "sess-a, sess-b")
                            self.assertIn("  modified: keep-custom-value" + newline, content)
                            self.assertIn("  originSessionId: keep-custom-session" + newline, content)
                            self.assertEqual(audit.split_frontmatter_raw(content)[1],
                                             audit.split_frontmatter_raw(extract["content"])[1])
                    again, dropped = assemble.assemble_proposals([cluster], drafts, live_root, self.STAMP)
                    self.assertEqual(dropped, [])
                    self.assertEqual(again, proposals)
                    self.assertEqual(audit.content_id(again), audit.content_id(proposals))
                    self.assertEqual((live / "mega.md").read_bytes(), donor.encode())

    def test_drafter_identity_removed_when_donor_has_no_frontmatter(self):
        # preserve_metadata returns the drafter unchanged on this fallback path.
        with tempfile.TemporaryDirectory() as temp:
            live_root, live = self._project(Path(temp), {"mega.md": "Plain source prose.\n"})
            cluster = self._cluster(["mega.md"], live)
            draft = self._draft()
            proposals, dropped = assemble.assemble_proposals(
                [cluster], {cluster["cluster_id"]: [draft]}, live_root, self.STAMP
            )
            self.assertEqual(dropped, [])
            self.assertEqual(proposals[0]["results"][0]["content"], draft["survivor"]["content"])
            for result, extract in zip(proposals[0]["results"][1:], draft["extracts"]):
                self._assert_no_identity(result["content"])
                self.assertEqual(audit.split_frontmatter_raw(result["content"])[1],
                                 audit.split_frontmatter_raw(extract["content"])[1])

    def test_build_results_and_content_bound_manifest_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            donor = self.HEADER + "node_type: memory\ntype: project\n" + self.IDENTITY + "---\nSource prose.\n"
            live_root, live = self._project(root, {"mega.md": donor})
            cluster = self._cluster(["mega.md"], live)
            (root / "plan.json").write_text(json.dumps({"clusters": [cluster]}), encoding="utf-8")
            draft = self._draft()
            drafts_path = root / "drafts.json"

            def write_drafts():
                drafts_path.write_text(json.dumps({"clusters": [{
                    "cluster_id": cluster["cluster_id"], "proposals": [draft]
                }]}), encoding="utf-8")
                fixtures.write_findings(root)

            write_drafts()
            manifests = []
            for name in ("first", "repeat", "edited"):
                if name == "edited":
                    draft["extracts"][0]["content"] += "Additional durable detail.\n"
                    write_drafts()
                out = root / name
                result = fixtures.run_cli(
                    "build", "--live-root", str(live_root), "--plan", str(root / "plan.json"),
                    "--drafts", str(drafts_path), "--findings", str(root / "findings.json"),
                    "--out", str(out), "--created-at-line", "0", "--stamp", self.STAMP,
                    env=fixtures.subprocess_env(root / "claude-config"),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
                manifests.append(manifest)
                self.assertEqual(manifest["id"], audit.content_id(manifest["proposals"]))
                for result in manifest["proposals"][0]["results"]:
                    self.assertEqual((out / "results" / f"proj__{result['path']}").read_bytes(), result["content"].encode())
                    if result["path"] != "mega.md":
                        self._assert_no_identity(result["content"])
            self.assertEqual(manifests[0], manifests[1])
            self.assertNotEqual(manifests[1]["id"], manifests[2]["id"])
