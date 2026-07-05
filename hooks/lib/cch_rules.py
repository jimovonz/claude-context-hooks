"""Glob-scoped rules — Cursor-style "Auto Attached" instructions for CCH.

Rule files live in `<project>/.cch/rules/*.md` with minimal frontmatter:

    ---
    globs: prisma/**, *.prisma
    ---
    Schema changes need a manual prod migration: ...

When a wrapped Bash command touches a file matching a rule's globs, the rule
body is appended to the command output as a footer (same channel as the
cairn-graph footer) — ONCE per session per rule, deduped via marker files, so
repeated reads never re-pay the tokens. Matching uses fnmatch against the
project-relative path and the basename; `*` crosses directory separators
(lenient, Cursor-style).
"""
import hashlib
import os
import re
import shlex
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

SEEN_DIR = Path.home() / '.claude' / 'cache' / 'cch' / 'rules-seen'
MAX_RULE_BYTES = 4000       # a rule is a nudge, not a novel
MARKER_TTL_S = 7 * 86400    # prune dedupe markers after a week


def _session_key() -> str:
    sid = os.environ.get('CCH_SESSION_ID', '')
    if sid:
        return sid[:32]
    # No session id (direct cache-wrap / cch-batch invocation): dedupe per-day
    # so a rule fires at most daily rather than being suppressed forever.
    return 'day-' + time.strftime('%Y%m%d')


def _rules_dir(cwd: str) -> Optional[Path]:
    """Nearest .cch/rules directory at or above cwd (stops at $HOME or /)."""
    home = Path.home()
    p = Path(cwd).resolve()
    for candidate in [p, *p.parents]:
        d = candidate / '.cch' / 'rules'
        if d.is_dir():
            return d
        if candidate == home:
            break
    return None


def _parse_rule(path: Path) -> Optional[tuple[list[str], str]]:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')[:MAX_RULE_BYTES]
    except OSError:
        return None
    m = re.match(r'\s*---\s*\n(.*?)\n---\s*\n?(.*)', text, re.S)
    if not m:
        return None
    globs = []
    for line in m.group(1).splitlines():
        k, _, v = line.partition(':')
        if k.strip().lower() == 'globs':
            globs = [g.strip() for g in v.split(',') if g.strip()]
    body = m.group(2).strip()
    return (globs, body) if globs and body else None


def _candidate_paths(command: str, cwd: str) -> list[Path]:
    """Existing files referenced by the command (bounded)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    out = []
    for t in tokens[:40]:
        if t.startswith('-') or len(t) < 2 or '://' in t:
            continue
        p = Path(t) if os.path.isabs(t) else Path(cwd) / t
        try:
            if p.is_file():
                out.append(p.resolve())
        except OSError:
            continue
        if len(out) >= 8:
            break
    return out


def _matches(rel: str, name: str, globs: list[str]) -> bool:
    return any(fnmatch(rel, g) or fnmatch(name, g) for g in globs)


def _already_seen(rule: Path) -> bool:
    SEEN_DIR.mkdir(parents=True, exist_ok=True)
    key = f'{_session_key()}__{hashlib.blake2s(str(rule).encode(), digest_size=8).hexdigest()}'
    marker = SEEN_DIR / key
    if marker.exists():
        return True
    marker.touch()
    # opportunistic prune
    try:
        cutoff = time.time() - MARKER_TTL_S
        for m in SEEN_DIR.iterdir():
            if m.stat().st_mtime < cutoff:
                m.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def rules_footer(command: str, cwd: str) -> Optional[str]:
    """Rule bodies triggered by this command's file targets, or None."""
    rules_dir = _rules_dir(cwd)
    if not rules_dir:
        return None
    paths = _candidate_paths(command, cwd)
    if not paths:
        return None
    project_root = rules_dir.parent.parent
    lines = []
    for rule_file in sorted(rules_dir.glob('*.md')):
        parsed = _parse_rule(rule_file)
        if not parsed:
            continue
        globs, body = parsed
        for p in paths:
            try:
                rel = str(p.relative_to(project_root))
            except ValueError:
                rel = p.name
            if _matches(rel, p.name, globs):
                if not _already_seen(rule_file):
                    lines.append(f'[cch-rule: {rule_file.name} — touched {rel}]\n{body}')
                break
    return '\n\n'.join(lines) if lines else None
