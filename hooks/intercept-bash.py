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
import shlex
import sys
from pathlib import Path

WRAPPER_PATH = Path.home() / '.claude' / 'hooks' / 'cache-wrap.py'

PASSTHROUGH_MARKERS = (
    'cache-wrap.py',
    'ccm-get.py',
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
