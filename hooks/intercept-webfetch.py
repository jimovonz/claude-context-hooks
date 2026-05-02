#!/usr/bin/env python3
"""
PreToolUse:WebFetch — block, redirect to `curl` via Bash so output flows
through the cache-wrap pipeline.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event

REASON = (
    "Use Bash with curl instead so output is RTK-compressed and cached. "
    "Equivalent invocations:\n"
    "  curl -sSL URL                       # follow redirects, silent\n"
    "  curl -sSL URL | rtk html            # strip to readable text\n"
    "  curl -sSLI URL                      # headers only\n"
    "  curl -sSL -o file URL               # save to disk\n"
    "Pipe through rtk for HTML→markdown when fetching pages."
)


def main() -> int:
    if os.environ.get('CCH_DISABLE') == '1':
        sys.stdout.write('{}\n')
        return 0
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    url = (data.get('tool_input') or {}).get('url', '')
    log_event('deny_webfetch', url=url[:200])
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
