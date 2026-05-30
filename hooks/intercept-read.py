#!/usr/bin/env python3
"""
PreToolUse:Read — multimodal-only allowlist.

Allowed: image/document extensions whose Bash equivalents return binary
garbage or lose structure. The multimodal model interprets these via
tool_result image content blocks that only built-in Read produces.

Denied: everything else. Inspect via Bash (cat / head / sed / rg) so
output is RTK-compressed and large residuals cached for slice retrieval.
Edit via cch-edit (Bash-routed) so the read-before-edit guard never
fires and the cache wrapper is preserved on every file.

No two-strike, no reason gate, no per-session state. The guard's only
job here is routing — Edit no longer needs built-in Read because
cch-edit replaces built-in Edit.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event

MULTIMODAL_EXTS = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp',
    '.pdf', '.ipynb', '.svg',
}

REASON_TEMPLATE = (
    "BLOCKED: Use cairn-graph --location SYMBOL first, then sed -n 'A,Bp' {path}.\n"
    "Edit: cch-edit.py {path} 'old' 'new'"
)


def _ext_lower(path: str) -> str:
    return Path(path).suffix.lower()


def _is_multimodal(path: str) -> bool:
    return _ext_lower(path) in MULTIMODAL_EXTS


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

    file_path = (data.get('tool_input') or {}).get('file_path', '')
    if not file_path:
        return _allow()

    if _is_multimodal(file_path):
        return _allow()

    try:
        st_size = os.stat(file_path).st_size
    except OSError:
        st_size = 0
    log_event('deny_read', path=file_path, st_size=st_size)
    return _deny(REASON_TEMPLATE.format(path=file_path))


if __name__ == '__main__':
    sys.exit(main())
