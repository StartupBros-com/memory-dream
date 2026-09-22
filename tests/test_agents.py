"""Agent permissions must stay explicit even for prompt-only tasks."""

import json
import unittest
from pathlib import Path

from memory_dream.audit import parse_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOOLS = {
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Task",
    "WebFetch",
    "WebSearch",
}


class AgentDeclarationsTests(unittest.TestCase):
    def agents(self):
        paths = sorted((REPO_ROOT / "agents").glob("*.md"))
        self.assertTrue(paths, "the plugin must declare its restricted agents")
        for path in paths:
            metadata, error, _body, _raw = parse_frontmatter(
                path.read_text(encoding="utf-8")
            )
            self.assertIsNone(error, f"{path.name}: {error}")
            yield path.name, metadata

    def test_agents_declare_nonempty_minimal_tool_lists(self):
        for name, metadata in self.agents():
            with self.subTest(agent=name):
                # Claude Code accepts comma-separated tool names; recognize
                # JSON flow lists too so the former empty-list bug fails here.
                declaration = metadata.get("tools", "")
                tools = (
                    json.loads(declaration)
                    if declaration.startswith("[")
                    else [
                        tool.strip() for tool in declaration.split(",") if tool.strip()
                    ]
                )
                self.assertIsInstance(tools, list)
                self.assertTrue(tools, "an empty tool list grants every inherited tool")
                self.assertTrue(all(isinstance(tool, str) and tool for tool in tools))
                self.assertFalse(FORBIDDEN_TOOLS.intersection(tools))
                self.assertLessEqual(set(tools), {"Read", "Grep", "Glob"})

    def test_agents_omit_host_claude_md(self):
        for name, metadata in self.agents():
            with self.subTest(agent=name):
                self.assertEqual(metadata.get("omitClaudeMd"), "true")


if __name__ == "__main__":
    unittest.main()
