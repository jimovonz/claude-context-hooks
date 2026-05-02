#!/usr/bin/env python3
"""
PreToolUse:Glob — block, redirect to `fd` (or `find`) via Bash so output
flows through the cache-wrap pipeline.
"""

import json
import os
import sys

REASON = (
    "Use Bash with fd (or find) instead so output is RTK-compressed and cached. "
    "Equivalent invocations:\n"
    "  fd PATTERN PATH                     # name match (regex by default)\n"
    "  fd -e py PATTERN                    # extension filter\n"
    "  fd -t f PATTERN                     # files only\n"
    "  find PATH -name 'GLOB' -type f      # if fd is unavailable\n"
    "Pipe to head/wc when only counts/first-N are needed."
)


def main() -> int:
    if os.environ.get('CCH_DISABLE') == '1':
        sys.stdout.write('{}\n')
        return 0
    try:
        json.load(sys.stdin)
    except json.JSONDecodeError:
        pass
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
