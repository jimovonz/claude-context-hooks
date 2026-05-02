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

WRAPPER_PATH = Path.home() / '.claude' / 'hooks' / 'cache-wrap.py'

PASSTHROUGH_MARKERS = (
    'cache-wrap.py',
    'ccm-get.py',
)


_SYMBOL_GREP_RE = re.compile(
    r"""(?:grep|rg)\b.*(?:"""
    r"""'def\s+(\w+)'|"def\s+(\w+)"|"""
    r"""'class\s+(\w+)'|"class\s+(\w+)")"""
)

_SESSION_MARKER = Path(tempfile.gettempdir()) / f"cch-graph-redirected-{os.getppid()}"


def _check_symbol_grep(cmd: str) -> str | None:
    """Detect grep-for-symbol patterns. Returns redirect message or None.
    Only fires once per session (marker file tracks).
    """
    # Skip if graph.db unlikely to exist (cairn-graph not on PATH)
    if not shutil.which("cairn-graph"):
        return None

    # Once per session: skip if already redirected
    if _SESSION_MARKER.exists():
        return None

    m = _SYMBOL_GREP_RE.search(cmd)
    if not m:
        return None

    symbol = next(g for g in m.groups() if g is not None)

    # Mark as redirected for this session
    try:
        _SESSION_MARKER.touch()
    except OSError:
        pass

    return (
        f"Symbol lookup detected. Use cairn-graph instead of grep:\\n"
        f"  cairn-graph --location {symbol}    # exact file:line\\n"
        f"  cairn-graph --callers {symbol}     # what calls it\\n"
        f"  cairn-graph --impact {symbol}      # blast radius\\n"
        f"Graph queries are faster (<15ms) and more precise than grep."
    )


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

    # Detect symbol-lookup-via-grep and redirect to cairn-graph (once per session)
    redirect = _check_symbol_grep(cmd)
    if redirect:
        response = {
            'permissionDecision': 'deny',
            'reason': redirect,
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
