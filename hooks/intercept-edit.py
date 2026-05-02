#!/usr/bin/env python3
"""
PreToolUse:Edit — hard-deny with redirect to cch-edit.

Built-in Edit requires a prior Read of the same file (read-before-edit
guard). That Read pulls the full file into context uncompressed and
bypasses the cache wrapper, undoing the cache savings for the very
files being edited. Routing edits through Bash via cch-edit avoids
this — Bash writes do not trigger the read-before-edit guard, and the
helper replicates Edit's safety contract (literal-string match,
uniqueness check, atomic write, diff output).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event

REASON = (
    "Built-in Edit denied — routes through Bash via cch-edit to avoid\n"
    "the read-before-edit guard pulling the full file into context\n"
    "(which would undo the cache wrapper for the file being edited).\n"
    "\n"
    "For {path}:\n"
    "  cch-edit.py {path} 'old_string' 'new_string'\n"
    "  cch-edit.py {path} 'old' 'new' --all                  # replace all\n"
    "  cch-edit.py {path} --old-file F1 --new-file F2        # multi-line\n"
    "\n"
    "cch-edit replicates the built-in's safety contract: literal-string\n"
    "match (no regex), errors on missing or non-unique old_string,\n"
    "atomic write, prints unified diff on success."
)


def _allow() -> int:
    sys.stdout.write('{}\n')
    return 0


def _deny(reason: str) -> int:
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        }
    }
    json.dump(response, sys.stdout)
    sys.stdout.write('\n')
    return 0


def main() -> int:
    if os.environ.get('CCH_DISABLE') == '1':
        return _allow()

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return _allow()

    file_path = (data.get('tool_input') or {}).get('file_path', '<path>')
    try:
        st_size = os.stat(file_path).st_size
    except OSError:
        st_size = 0
    log_event('deny_edit', path=file_path, st_size=st_size)
    return _deny(REASON.format(path=file_path))


if __name__ == '__main__':
    sys.exit(main())
