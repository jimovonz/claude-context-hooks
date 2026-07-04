#!/usr/bin/env python3
"""
PreToolUse:Bash hook — wraps the command in cache-wrap.py via
hookSpecificOutput.updatedInput.command.

Layering: this hook is registered AFTER any RTK rewrite hook in
settings.json. Claude Code fires PreToolUse:Bash hooks in the order
they appear and propagates updatedInput between them, so by the time
this hook reads tool_input.command it is already the RTK-rewritten
form (e.g. `rtk git status` instead of `git status`).

We then rewrite once more to wrap the (possibly rtk-rewritten) command
in cache-wrap.py, which executes it and decides inline-vs-cache after
seeing real output size.

Exemptions (passed through unchanged):
- ccm-get.py invocations (already a cache retrieval)
- cache-wrap.py invocations (already wrapped — no double-wrap)
- Empty commands

The wrapper itself executes via `bash -c`, so all shell features work.
"""

import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from lib.event_log import log_event

WRAPPER_PATH = Path.home() / '.claude' / 'hooks' / 'cache-wrap.py'

PASSTHROUGH_MARKERS = (
    'cache-wrap.py',
    'ccm-get.py',
    'cch-batch.py',
)

_CODE_EXTS = frozenset({
    '.py', '.js', '.ts', '.tsx', '.jsx', '.rs', '.go',
    '.java', '.rb', '.c', '.cpp', '.h', '.hpp', '.cs',
    '.ex', '.exs',
})

_BULK_THRESHOLD = 50

_BULK_WARN_THRESHOLD = 100

_BULK_WARN_PREFIX = "[cch: reading {lines} lines from {path} — consider cairn-graph --location SYMBOL for targeted reads]"

_BULK_READ_REDIRECT = (
    "BLOCKED: Run cairn-graph --location SYMBOL first, then sed -n 'A,Bp' on the result."
)


# Definition keywords across languages
_DEF_KEYWORDS = (
    r"def|class|function|fn|func|type|interface|struct|enum|trait|impl|module"
)

# Pattern categories: (compiled_regex, redirect_type)
_PATTERNS = [
    # 1. Symbol definition: grep 'def foo' / grep "class Bar" / etc.
    (re.compile(
        rf"""(?:grep|rg)\b.*(?:'|")(?:{_DEF_KEYWORDS})\s+(\w+)(?:'|")"""
    ), "location"),
    # 2. Caller search: grep 'foo(' or grep '\.foo('
    (re.compile(
        r"""(?:grep|rg)\b.*(?:'|")\\?\.?(\w{2,})\((?:'|")"""
    ), "callers"),
    # 3. Test discovery: grep 'test_foo' / rg test_foo (quoted or unquoted argv)
    (re.compile(
        r"""(?:grep|rg)\b.*?(?:['"]|\s)(?:def\s+)?(test_\w+)(?:['"]|\s|$)"""
    ), "tests"),
    # 4. Import tracing: grep 'from foo import' or grep 'import foo'
    (re.compile(
        r"""(?:grep|rg)\b.*(?:'|")(?:from\s+(\w+)\s+import|import\s+(\w+))(?:'|")"""
    ), "callees"),
]

_SESSION_MARKER = Path(tempfile.gettempdir()) / f"cch-graph-redirected-{os.getppid()}"

_REDIRECT_TEMPLATES = {
    "location": "BLOCKED: Use cairn-graph --location {symbol} instead of grep.",
    "callers": "BLOCKED: Use cairn-graph --callers {symbol} instead of grep.",
    "tests": "BLOCKED: Use cairn-graph --tests {symbol} instead of grep.",
    "callees": "BLOCKED: Use cairn-graph --callees {symbol} instead of grep.",
}


def _extract_code_file(cmd_tail: str) -> str | None:
    """Return the last non-flag token if it has a code extension."""
    for tok in reversed(cmd_tail.split()):
        if not tok.startswith('-'):
            if Path(tok).suffix.lower() in _CODE_EXTS:
                return tok
            return None
    return None


def _check_bulk_read(cmd: str) -> str | None:
    """Detect bulk reads of code files. Returns redirect message or None.

    Unconditional — no session marker. cat/head/tail/sed of an entire
    code file is never optimal; sed -n with a narrow range always works.
    """
    first_segment = cmd.split('|')[0] if '|' in cmd else cmd
    effective = re.sub(r'^rtk\s+', '', first_segment.strip())

    # cat <code-file> — always a full dump
    if re.match(r'^cat\b', effective):
        if _extract_code_file(effective[3:]):
            return _BULK_READ_REDIRECT
        return None

    # head/tail with large -n
    m = re.match(r'^(head|tail)\b(.*)', effective)
    if m:
        rest = m.group(2)
        n_match = re.search(r'-n\s*(\d+)|-(\d+)', rest)
        if not n_match:
            return None  # no -n = default 10, fine
        n = int(n_match.group(1) or n_match.group(2))
        if n < _BULK_THRESHOLD:
            return None
        if _extract_code_file(rest):
            return _BULK_READ_REDIRECT
        return None

    return None


def _warn_bulk_sed(cmd: str) -> str | None:
    """Return a warning prefix for large sed -n reads on code files, or None."""
    effective = re.sub(r'^rtk\s+', '', cmd.strip())
    m = re.match(r"""^sed\s+-n\s+['"]?(\d+),(\d+)p['"]?(.*)""", effective)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    lines = b - a
    if lines < _BULK_WARN_THRESHOLD:
        return None
    path = _extract_code_file(m.group(3))
    if not path:
        return None
    return _BULK_WARN_PREFIX.format(lines=lines, path=path)


def _graph_db_for(cwd: str) -> Path | None:
    """Walk up from cwd to find .code-review-graph/graph.db."""
    d = Path(cwd or '.').resolve()
    while True:
        candidate = d / '.code-review-graph' / 'graph.db'
        if candidate.is_file():
            return candidate
        if d.parent == d:
            return None
        d = d.parent


def _graph_answer(graph_db: Path, redirect_type: str, symbol: str) -> str | None:
    """Answer a symbol query straight from graph.db, or None if unresolvable.

    Only returns a string when the graph genuinely has the answer — a miss
    must NOT block the grep (the graph may be stale or the language's edge
    extraction thin, e.g. Kotlin call edges).
    """
    try:
        conn = sqlite3.connect(str(graph_db))
        conn.execute("PRAGMA busy_timeout=500")
        if redirect_type == "location":
            rows = conn.execute(
                "SELECT file_path, line_start, line_end FROM nodes "
                "WHERE name = ? AND kind IN ('Function', 'Class', 'Type') "
                "ORDER BY line_start LIMIT 3",
                (symbol,),
            ).fetchall()
            if rows:
                locs = " · ".join(f"{f}:{a}-{b}" for f, a, b in rows)
                return (
                    f"graph: {symbol} → {locs}. "
                    f"Body: sed -n 'A,Bp' on that span. "
                    f"(grep skipped — rerun only if you need every text occurrence)"
                )
        elif redirect_type in ("callers", "tests", "callees"):
            edge_kind = {"callers": "CALLS", "tests": "TESTED_BY",
                         "callees": "CALLS"}[redirect_type]
            col, other = (("target_qualified", "source_qualified")
                          if redirect_type != "callees"
                          else ("source_qualified", "target_qualified"))
            rows = conn.execute(
                f"SELECT DISTINCT {other} FROM edges "
                f"WHERE kind = ? AND ({col} LIKE ? OR {col} = ? "
                f"OR {col} LIKE ?) LIMIT 6",
                (edge_kind, f"%::{symbol}", symbol, f"%.{symbol}"),
            ).fetchall()
            if rows:
                names = " · ".join(r[0].rsplit("::", 1)[-1] for r in rows)
                return (
                    f"graph: {symbol} {redirect_type}: {names} "
                    f"(cairn-graph --{redirect_type} {symbol} for locations; "
                    f"grep skipped)"
                )
        return None
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _check_symbol_grep(cmd: str, cwd: str = '') -> str | None:
    """Detect grep-for-symbol patterns and answer them from the graph.

    Resolves the symbol at hook time: a hit blocks the grep WITH the answer
    (no redirect round-trip); a graph miss passes the grep through silently
    (never redirect to a graph that cannot answer).
    """
    for pattern, redirect_type in _PATTERNS:
        m = pattern.search(cmd)
        if not m:
            continue
        symbol = next(g for g in m.groups() if g is not None)
        graph_db = _graph_db_for(cwd)
        if graph_db is None:
            return None
        answer = _graph_answer(graph_db, redirect_type, symbol)
        if answer:
            return f"BLOCKED: {answer}"
        return None

    return None


def should_skip_wrap(cmd: str) -> bool:
    if not cmd or not cmd.strip():
        return True
    for marker in PASSTHROUGH_MARKERS:
        if marker in cmd:
            return True
    return False


def main() -> int:
    if os.environ.get('CCH_DISABLE') == '1':
        sys.stdout.write('{}\n')
        return 0

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.stdout.write('{}\n')
        return 0

    tool_input = data.get('tool_input') or {}
    cmd = tool_input.get('command', '')

    if should_skip_wrap(cmd):
        sys.stdout.write('{}\n')
        return 0

    # Detect bulk code-file reads (unconditional — no session marker)
    redirect = _check_bulk_read(cmd)
    if redirect:
        response = {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': redirect,
            }
        }
        json.dump(response, sys.stdout)
        sys.stdout.write('\n')
        return 0

    # Soft-warn on large sed -n reads (non-blocking — prepends warning to output)
    warn_msg = _warn_bulk_sed(cmd)
    if warn_msg:
        log_event('warn_bulk_sed', cmd_head=cmd[:120])
        cmd = f'echo "{warn_msg}"; {cmd}'

    # Symbol-lookup-via-grep: answer from the graph at hook time (block with
    # the answer); pass through silently when the graph cannot resolve it
    redirect = _check_symbol_grep(cmd, data.get('cwd') or os.getcwd())
    if redirect:
        response = {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': redirect,
            }
        }
        json.dump(response, sys.stdout)
        sys.stdout.write('\n')
        return 0

    sid = str(data.get('session_id') or '')[:36]
    env_prefix = f'CCH_SESSION_ID={shlex.quote(sid)} ' if sid else ''
    wrapped = f'{env_prefix}{WRAPPER_PATH} -- {shlex.quote(cmd)}'

    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'updatedInput': {
                **tool_input,
                'command': wrapped,
            },
        }
    }
    json.dump(response, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
