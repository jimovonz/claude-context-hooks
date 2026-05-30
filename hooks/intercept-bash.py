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


def _check_symbol_grep(cmd: str) -> str | None:
    """Detect grep-for-symbol patterns. Returns redirect message or None.
    Only fires once per session (marker file tracks).
    """
    if not shutil.which("cairn-graph"):
        return None

    if _SESSION_MARKER.exists():
        return None

    for pattern, redirect_type in _PATTERNS:
        m = pattern.search(cmd)
        if m:
            symbol = next(g for g in m.groups() if g is not None)
            try:
                _SESSION_MARKER.touch()
            except OSError:
                pass
            return _REDIRECT_TEMPLATES[redirect_type].format(symbol=symbol)

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

    # Detect symbol-lookup-via-grep and redirect to cairn-graph (once per session)
    redirect = _check_symbol_grep(cmd)
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

    wrapped = f'{WRAPPER_PATH} -- {shlex.quote(cmd)}'

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
