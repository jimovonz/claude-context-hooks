#!/usr/bin/env python3
"""
PreToolUse:Glob — block, redirect to `fd` (or `find`) via Bash so output
flows through the cache-wrap pipeline. Falls back to POSIX `find` when fd
is not installed so the redirect never dead-ends.
"""

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event


def _reason() -> str:
    """Suggest fd when it is on PATH, else fall back to POSIX find so a
    machine without fd still gets a runnable redirect."""
    if shutil.which('fd'):
        return "BLOCKED: Use fd PATTERN PATH (or find PATH -name 'GLOB' -type f)."
    return (
        "BLOCKED: Use find PATH -name 'GLOB' -type f. "
        "[fd not on PATH — install fd-find for nicer syntax]"
    )


def main() -> int:
    if os.environ.get('CCH_DISABLE') == '1':
        sys.stdout.write('{}\n')
        return 0
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    ti = data.get('tool_input') or {}
    log_event('deny_glob', pattern=str(ti.get('pattern', ''))[:80], path=ti.get('path', ''))
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': _reason(),
        }
    }
    json.dump(response, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
