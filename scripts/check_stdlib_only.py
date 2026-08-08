#!/usr/bin/env python3
"""Assert that memory_dream/ imports nothing but the standard library.

Zero runtime dependencies is a contract of this plugin: memory_dream/
installs with the plugin and runs on a bare Python 3.10+ install, so
`pip install` is never a prerequisite for the shipped CLI. Nothing enforces
that at import time -- a third-party import fails loudly the first time
someone runs the tool on a machine without it installed, not at review time
-- so this walks the AST instead of trusting the diff.

It also asserts that the two platform-lock modules (`fcntl`, `msvcrt`) only
appear inside compat.py: that module is the one place platform-specific code
is allowed to live, and every other module is expected to go through its
shims instead of reaching for the platform primitive directly.

This is a repo-maintenance tool. It is not part of the shipped plugin, makes
no model calls, and imports only stdlib itself.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "memory_dream"

# Sibling modules import each other by name (`from memory_dream import audit`),
# and every module is free to import the package itself (`from memory_dream
# import config`, `import memory_dream`). The allowlist is derived from what
# is actually on disk, never hardcoded: a fixed list silently turns every new
# sibling module into a false positive on the day it's added, which is
# exactly the day nobody wants a spurious CI failure.
SIBLING_MODULES = {path.stem for path in PACKAGE_DIR.glob("*.py")} | {PACKAGE_DIR.name}

PLATFORM_ONLY_MODULES = {"fcntl", "msvcrt"}
PLATFORM_HOME = "compat.py"


def _imported_top_level_names(tree: ast.AST) -> set[str]:
    """Every module name reached by an import anywhere in the file.

    Walks the full tree (not just the module's syntactic top level) so an
    import tucked inside a function or an `if os.name == ...:` guard is still
    caught -- that is exactly how compat.py's platform shims are structured.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` / `from .foo import x` has no absolute module
            # name to police.
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def main() -> int:
    if not hasattr(sys, "stdlib_module_names"):  # pragma: no cover
        print("need Python 3.10+ to introspect the stdlib module list")
        return 2

    allowed = set(sys.stdlib_module_names) | SIBLING_MODULES
    offenders: list[str] = []
    platform_offenders: list[str] = []
    checked = 0

    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        checked += 1
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _imported_top_level_names(tree)

        for name in sorted(imports - allowed):
            offenders.append(f"{rel}: imports {name!r}")

        if path.name != PLATFORM_HOME:
            for name in sorted(imports & PLATFORM_ONLY_MODULES):
                platform_offenders.append(f"{rel}: imports {name!r} outside {PLATFORM_HOME}")

    if offenders:
        print("Third-party imports found under memory_dream/ (must be stdlib-only):")
        for line in offenders:
            print(f"  {line}")

    if platform_offenders:
        print(f"Platform-lock imports found outside {PLATFORM_HOME} (must stay quarantined there):")
        for line in platform_offenders:
            print(f"  {line}")

    if offenders or platform_offenders:
        return 1

    print(f"stdlib-only: OK ({checked} file(s) under memory_dream/ checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
