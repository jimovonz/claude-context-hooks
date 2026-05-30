#!/usr/bin/env python3
"""
PreToolUse:Grep — block, redirect to `rg` via Bash so output flows
through the cache-wrap pipeline.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event

REASON = (
    "BLOCKED: Use rg -n PATTERN PATH (or cairn-graph --callers/--tests/--location SYMBOL)."
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
    log_event('deny_grep', pattern=str(ti.get('pattern', ''))[:80], path=ti.get('path', ''))
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': REASON,
        }
    }
    json.dump(response, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
