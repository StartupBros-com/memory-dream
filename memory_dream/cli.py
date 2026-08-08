"""memory-dream: operator-gated consolidation for Claude Code auto-memory.

Runnable three ways, all equivalent:
  memory-dream <subcommand> ...              (pip/pipx console script)
  python3 -m memory_dream <subcommand> ...
  python3 <plugin-root>/memory_dream/cli.py <subcommand> ...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a file (the plugin invocation path)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_dream import __version__, config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-dream",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument("--version", action="version", version=f"memory-dream {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # Each module registers its own subcommands (audit/triage/fix, plan/build/
    # archive, trace/transcript-locate, apply/restore, eval). Imports live here
    # so `--version` and parser construction never depend on module import
    # side effects.
    from memory_dream import apply as apply_mod
    from memory_dream import assemble, audit, recall_eval, transcript

    audit.add_parsers(subparsers)
    assemble.add_parsers(subparsers)
    transcript.add_parsers(subparsers)
    apply_mod.add_parsers(subparsers)
    recall_eval.add_parsers(subparsers)
    _add_doctor_parser(subparsers)
    _add_scratch_parser(subparsers)
    _add_open_preview_parser(subparsers)
    return parser


def _add_doctor_parser(subparsers) -> None:
    p = subparsers.add_parser("doctor", help="preflight: report what works and what degrades here")
    config.add_root_args(p)
    p.set_defaults(func=_run_doctor)


def _add_scratch_parser(subparsers) -> None:
    p = subparsers.add_parser("scratch", help="print the resolved session scratch directory")
    p.set_defaults(func=lambda args: print(config.scratch_dir()) or 0)


def _add_open_preview_parser(subparsers) -> None:
    p = subparsers.add_parser("open-preview", help="open a patch set's preview.html in the operator's browser")
    p.add_argument("--patch-set", required=True, help="patch-set directory holding preview.html")
    p.set_defaults(func=_run_open_preview)


def _run_doctor(args) -> int:
    import importlib.util
    import os
    import shutil

    from memory_dream import transcript

    # (label, ok, detail, fatal). Only a fatal failure exits non-zero: the tool
    # cannot run at all (wrong Python, no writable scratch, broken lock backend,
    # or no memory root to operate on). Everything else — consent trace, mirror
    # freshness, crash leftovers, git/gh, the index-cap reminder — is advisory:
    # it reports how this environment degrades, not that the tool is broken. A
    # missing consent trace, in particular, only affects `apply`; the read-only
    # triage/plan/build/eval stages the quickstart leads with do not need it.
    checks: list[tuple[str, bool, str, bool]] = []

    ok = sys.version_info >= (3, 10)
    checks.append(("python", ok, f"{sys.version.split()[0]} (need >= 3.10)", True))

    live = Path(args.live_root).expanduser()
    projects = [p for p in live.iterdir() if (p / "memory").is_dir()] if live.is_dir() else []
    checks.append(
        ("live root", live.is_dir(), f"{live} — {len(projects)} project(s) with memory" if live.is_dir() else f"{live} missing", True)
    )

    if args.mirror_root:
        mirror = Path(args.mirror_root).expanduser()
        checks.append(("mirror mode", mirror.is_dir(), f"{mirror}" if mirror.is_dir() else f"{mirror} missing", False))
    else:
        checks.append(("snapshot mode", True, "no mirror configured; apply snapshots into the patch set (restore via `memory-dream restore`)", False))

    try:
        tdir = transcript.transcripts_dir_for(Path.cwd())
        found = tdir.is_dir()
        detail = f"{tdir}" if found else (
            f"{tdir} not found — consent trace unavailable from this cwd "
            "(only needed for `apply`; triage/plan/build/eval work without it)"
        )
        probe = transcript.schema_probe(tdir) if found else None
        if probe is not None:
            detail += f"; schema probe: {probe}"
        checks.append(("consent trace", found, detail, False))
    except Exception as exc:  # pragma: no cover - environment specific
        checks.append(("consent trace", False, str(exc), False))

    from memory_dream import compat

    scratch = config.scratch_dir()
    checks.append(("scratch dir", os.access(scratch, os.W_OK), str(scratch), True))
    lock_probe = scratch / ".doctor-lock-probe"
    try:
        with compat.FileLock(lock_probe):
            pass
        checks.append(("single-flight lock", True, f"{os.name} lock backend works", True))
    except Exception as exc:  # pragma: no cover - environment specific
        checks.append(("single-flight lock", False, str(exc), True))

    orphans = list(live.glob("*/memory/*.dream-tmp")) if live.is_dir() else []
    checks.append(
        ("staging leftovers", not orphans, f"{len(orphans)} orphaned *.dream-tmp file(s) — crash leftovers, review and remove" if orphans else "none", False)
    )

    for tool in ("git", "gh"):
        checks.append((f"{tool} (optional)", True, shutil.which(tool) or "not found — repo-grounding checks degrade to note-only", False))

    checks.append(
        (
            "index cap",
            True,
            f"{config.INDEX_LOAD_MAX_LINES} lines / {config.INDEX_LOAD_MAX_BYTES} bytes — measured against Claude Code v2.1.211; re-verify after CLI upgrades (docs/TUNING.md)",
            False,
        )
    )

    hard_failures = [c for c in checks if not c[1] and c[3]]
    advisories = [c for c in checks if not c[1] and not c[3]]
    for label, ok, detail, fatal in checks:
        mark = "ok  " if ok else ("FAIL" if fatal else "warn")
        print(f"{mark} {label}: {detail}")
    passed = len(checks) - len(hard_failures) - len(advisories)
    summary = f"doctor: {passed}/{len(checks)} checks passed"
    if advisories:
        summary += f", {len(advisories)} advisory (non-fatal)"
    if hard_failures:
        summary += f", {len(hard_failures)} FAIL"
    print(summary)
    return 1 if hard_failures else 0


def _run_open_preview(args) -> int:
    """Best-effort platform opener, including the WSL copy-to-Windows dance.

    The Windows-home copy holds memory bodies; the operator is told to delete
    it after review. Never fails the pipeline: a preview that does not open is
    reported, and the skill falls back to inline diffs.
    """
    import os
    import shutil
    import subprocess

    preview = Path(args.patch_set).expanduser() / "preview.html"
    if not preview.is_file():
        print(f"no preview.html under {args.patch_set}", file=sys.stderr)
        return 1

    def _try(cmd: list[str]) -> bool:
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            return True
        except Exception:
            return False

    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        os.startfile(preview)  # type: ignore[attr-defined]
        print(f"opened {preview}")
        return 0

    if Path("/mnt/c/Users").is_dir():  # WSL: browsers cannot read \\wsl$ paths reliably
        username = None
        try:
            out = subprocess.run(
                ["cmd.exe", "/c", "echo %USERNAME%"], capture_output=True, text=True, timeout=15
            )
            username = out.stdout.strip() or None
        except Exception:
            username = None
        candidates = [Path(f"/mnt/c/Users/{username}")] if username else []
        candidates += [p for p in Path("/mnt/c/Users").iterdir() if p.is_dir() and p.name not in ("Public", "Default", "Default User", "All Users")]
        for home in candidates:
            target = home / "memory-dream-preview.html"
            try:
                shutil.copy(preview, target)
            except OSError:
                continue
            win_path = subprocess.run(["wslpath", "-w", str(target)], capture_output=True, text=True).stdout.strip()
            if _try(["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{win_path}'"]):
                print(f"opened in browser (copy at {target} — it holds note bodies; delete after review)")
                return 0

    for opener in (["wslview"], ["open"], ["xdg-open"]):
        if shutil.which(opener[0]) and _try(opener + [str(preview)]):
            print(f"opened {preview}")
            return 0

    print(f"could not open a browser; review {preview} manually", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    config.load_file_config()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
