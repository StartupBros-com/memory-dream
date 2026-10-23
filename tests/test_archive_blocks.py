"""Archive CLI regressions using synthetic, isolated memory trees only."""
import difflib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_apply import Harness, REPO_ROOT, _clean_env, note

POINTER = '- Older or resolved entries are archived in MEMORY-archive.md (not auto-loaded); grep it when a topic is missing here.\n'


class ArchiveBlockTests(unittest.TestCase):
    def test_build_and_apply_preserve_shared_retained_lines(self):
        for hot_date, keep, copies in [('2026-07-19', [], 1), ('2026-06-01', ['--keep', 'hot.md'], 1), ('', [], 1), ('2026-07-19', [], 2)]:
            with self.subTest(hot_date=hot_date), tempfile.TemporaryDirectory() as temp:
                h = Harness(Path(temp))
                live = h.project()
                cold = '- [Cold](cold.md) — 2026-06-01\n  Keep rollback instructions handy.'
                hot = f'- [Hot](hot.md) — {hot_date}\n  Keep rollback instructions handy.\n  Keep rollback instructions handy.'
                original = '# Index\n' + (cold + '\n') * copies + hot + '\n\n# References\n\n'
                expected = '# Index\n' + hot + '\n\n# References\n\n' + POINTER
                (live / 'MEMORY.md').write_text(original, encoding='utf-8')
                for name in ('cold', 'hot'):
                    (live / f'{name}.md').write_text(note(name), encoding='utf-8')
                h.mirror_sync()
                result = subprocess.run([
                    sys.executable, '-m', 'memory_dream', 'archive',
                    '--live-root', str(h.live_root), '--mirror-root', str(h.mirror_root),
                    '--project', 'proj', '--cutoff', '2026-06-25',
                    '--out', str(h.patch_set), '--created-at-line', '2', *keep,
                ], cwd=REPO_ROOT, env=_clean_env(h.claude_config_dir), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest_path = h.patch_set / 'manifest.json'
                manifest_text = manifest_path.read_text(encoding='utf-8')
                manifest = json.loads(manifest_text)
                proposal = manifest['proposals'][0]
                self.assertEqual(proposal['archive_entries'], [cold] * copies)
                self.assertEqual((h.patch_set / 'archive-additions.txt').read_text(encoding='utf-8'), (cold + '\n') * copies)
                preview = (h.patch_set / 'index-proj.diff').read_text(encoding='utf-8')
                expected_diff = ''.join(difflib.unified_diff(original.splitlines(keepends=True), expected.splitlines(keepends=True), fromfile='a/MEMORY.md', tofile='b/MEMORY.md'))
                # Set up synthetic operator consent for the actual built manifest.
                h.add_proposal(proposal)
                h.write()
                manifest_path.write_text(manifest_text, encoding='utf-8')
                result = h.run(h.selection([proposal['id']]))
                self.assertEqual(result.returncode, 0, result.stderr)
                with self.subTest(stage='preview'):
                    self.assertEqual(preview, expected_diff)
                with self.subTest(stage='apply'):
                    self.assertEqual((live / 'MEMORY.md').read_text(encoding='utf-8'), expected)
                self.assertEqual((live / 'MEMORY-archive.md').read_text(encoding='utf-8'), '# Archived memory index entries (not auto-loaded)\n' + (cold + '\n') * copies)
                for name in ('cold', 'hot'):
                    self.assertEqual((live / f'{name}.md').read_text(encoding='utf-8'), note(name))

    def test_apply_consumes_only_complete_distinct_block_occurrences(self):
        short = '- [Cold](cold.md) — 2026-06-01'
        long = short + '\n  Continuation.'
        cases = [
            ('one duplicate', short + '\n' + short + '\n', [short], short + '\n' + POINTER),
            ('both duplicates', short + '\n' + short + '\n', [short, short], POINTER),
            ('too many duplicates', short + '\n', [short, short], None),
            ('long block before prefix', long + '\n\n' + short + '\n', [short], long + '\n\n' + POINTER),
            ('prefix before long block', short + '\n\n' + long + '\n', [long], short + '\n\n' + POINTER),
            ('prefix only', long + '\n', [short], None),
            ('mid-line substring', '- [Prefix](p.md) ' + short + '\n', [short], None),
            ('continuation only', long + '\n', ['  Continuation.'], None),
            ('empty entry', short + '\n', [''], None),
            ('no final newline', short, [short], POINTER),
            ('existing pointer', POINTER + short + '\n\n', [short], POINTER + '\n'),
            ('headings and spaces', '# Index\n\n' + short + '\n  \n## Next\n\n', [short], '# Index\n\n  \n## Next\n\n' + POINTER),
        ]
        for label, original, entries, expected in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp:
                h = Harness(Path(temp))
                live = h.project()
                (live / 'MEMORY.md').write_text(original, encoding='utf-8')
                h.mirror_sync()
                h.add_proposal({
                    'id': 'a1', 'project': 'proj', 'action': 'archive',
                    'sources': [{'path': 'MEMORY.md', 'digest': h.digest('proj', 'MEMORY.md')}],
                    'archive_entries': entries, 'results': [], 'deletes': [],
                    'sensitive': False, 'justification': 'synthetic occurrence test',
                })
                h.write()
                result = h.run(h.selection(['a1']))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((live / 'MEMORY.md').read_text(encoding='utf-8'), original if expected is None else expected)
                archive = live / 'MEMORY-archive.md'
                if expected is None:
                    self.assertFalse(archive.exists())
                    self.assertIn('archive-entries-not-found', (h.patch_set / 'apply-manifest.json').read_text(encoding='utf-8'))
                else:
                    self.assertEqual(archive.read_text(encoding='utf-8'), '# Archived memory index entries (not auto-loaded)\n' + '\n'.join(entries) + '\n')
