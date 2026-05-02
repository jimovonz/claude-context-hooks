#!/usr/bin/env python3
"""
PreToolUse:Write — hard-deny with redirect to cch-write.

Built-in Write requires a prior Read of any existing target (the
read-before-edit guard applies). That Read bypasses the cache wrapper.
Routing writes through Bash via cch-write avoids this — Bash writes
do not trigger the guard, and the helper provides atomic writes
(temp file + rename) plus parent-directory creation.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event

REASON = (
    "Built-in Write denied — routes through Bash via cch-write to avoid\n"
    "the read-before-edit guard pulling the existing file into context.\n"
    "\n"
    "For {path}:\n"
    "  echo 'content' | cch-write.py {path}\n"
    "  cat source.txt | cch-write.py {path}\n"
    "  cch-write.py {path} < /tmp/source\n"
    "\n"
    "  cch-write.py {path} << 'EOF'\n"
    "  multi-line content with $vars and `backticks` not expanded\n"
    "  EOF\n"
    "\n"
    "cch-write reads content from stdin (no shell-escaping required),\n"
    "writes atomically (temp + rename), and creates parent directories\n"
    "on demand."
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
    log_event('deny_write', path=file_path, st_size_existing=st_size)
    return _deny(REASON.format(path=file_path))


if __name__ == '__main__':
    sys.exit(main())
