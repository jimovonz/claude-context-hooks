#!/usr/bin/env python3
"""
PreToolUse:NotebookEdit — hard-deny with redirect to Bash JSON tooling.

NotebookEdit triggers the read-before-edit guard on the target .ipynb,
which pulls the full notebook (including all output cells) into context.
Notebooks are typically larger than source files because of embedded
output — this is the worst-case path for the cache bypass.

Edit notebooks via Bash:
  - jq for cell-level JSON manipulation
  - cch-edit.py on the .ipynb file directly (notebooks are JSON; if the
    target string is unique within the notebook, literal-match works)
  - python3 with nbformat for structured editing
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event

REASON = (
    "Built-in NotebookEdit denied — notebooks are large (embedded output\n"
    "cells) and the read-before-edit guard would pull the full .ipynb\n"
    "into context, the worst case for the cache bypass.\n"
    "\n"
    "For {path}, edit via Bash:\n"
    "  cch-edit.py {path} 'old_source_substring' 'new_source_substring'\n"
    "    (notebooks are JSON; literal match works if old_source is unique)\n"
    "\n"
    "  jq '.cells[N].source = [\"new line\\n\"]' {path} > {path}.new && mv {path}.new {path}\n"
    "    (cell-level edits via jq)\n"
    "\n"
    "  python3 -c 'import nbformat; nb = nbformat.read(\"{path}\", 4); ...; nbformat.write(nb, \"{path}\")'\n"
    "    (structured editing via nbformat)"
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

    file_path = (data.get('tool_input') or {}).get('notebook_path') \
        or (data.get('tool_input') or {}).get('file_path', '<path>')
    try:
        st_size = os.stat(file_path).st_size
    except OSError:
        st_size = 0
    log_event('deny_notebookedit', path=file_path, st_size=st_size)
    return _deny(REASON.format(path=file_path))


if __name__ == '__main__':
    sys.exit(main())
