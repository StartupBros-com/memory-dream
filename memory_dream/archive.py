"""Shared exact-entry archive transformation for preview and approved apply."""

from collections import Counter

ARCHIVE_POINTER = "- Older or resolved entries are archived in MEMORY-archive.md (not auto-loaded); grep it when a topic is missing here."


def _entry_spans(lines: list[str]):
    """Yield complete entry spans, excluding headings and blank separators."""
    start = None
    for position, line in enumerate(lines):
        if line.lstrip().startswith("- ["):
            if start is not None:
                yield start, position
            start = position
        elif start is not None and (not line.strip() or line.startswith("#")):
            yield start, position
            start = None
    if start is not None:
        yield start, len(lines)


def parse_index_entries(index_text: str) -> list[list[str]]:
    """Split MEMORY.md into entry blocks: a '- [' line plus its continuation lines."""
    lines = index_text.splitlines()
    return [lines[start:end] for start, end in _entry_spans(lines)]


def archived_index(index_text: str, entries: list[str]) -> str:
    """Remove one complete occurrence per requested entry, in source order.

    Match whole parsed blocks, never a prefix or an individual shared line.
    Duplicate requests consume distinct occurrences; insufficient occurrences
    raise before any writes. Preserve every byte outside the selected spans.
    """
    lines = index_text.splitlines(keepends=True)
    plain = index_text.splitlines()
    pending = Counter(entries)
    remaining = []
    cursor = 0
    for start, end in _entry_spans(plain):
        entry = "\n".join(plain[start:end])
        if pending[entry]:
            remaining.extend(lines[cursor:start])
            cursor = end
            pending[entry] -= 1
    if any(pending.values()):
        raise ValueError("archive entries not found as complete block occurrences")
    remaining.extend(lines[cursor:])
    new_index = "".join(remaining)
    if ARCHIVE_POINTER not in new_index.splitlines():
        if new_index and not new_index.endswith("\n"):
            new_index += "\n"
        new_index += ARCHIVE_POINTER + "\n"
    return new_index
