#!/usr/bin/env python3
"""
PreToolUse:Agent — redirect Explore agents to cairn-graph when the
prompt describes code-structure queries the graph can answer (<15ms
vs 50k+ tokens).

Non-Explore agents pass through unconditionally.
Explore agents pass through when the prompt targets non-code content
(docs, configs, logs) that the graph doesn't index.
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.event_log import log_event

# Patterns indicating code-structure queries cairn-graph can answer.
# Matched against the combined prompt+description text (case-insensitive).
_CODE_STRUCTURE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"(?:where|how)\s+is\s+\w+\s+(?:defined|implemented|declared)",
        r"(?:what|who)\s+calls\s+\w+",
        r"(?:find|locate|search\s+for)\s+(?:the\s+)?(?:definition|implementation|declaration)\b",
        r"(?:find|locate)\s+(?:the\s+)?(?:function|class|method|symbol)\b",
        r"(?:callers?|callees?|dependencies|dependents)\s+(?:of|for)\b",
        r"(?:test\s+coverage|which\s+tests|tests?\s+for)\b",
        r"read\s+(?:all|every|each)\s+(?:hook|intercept|helper)\s+(?:script|file)",
        r"read\s+(?:all|every|each)\s+(?:python|source|code)\s+files?\b",
        r"(?:explore|analyze|analyse)\s+(?:the\s+)?(?:hook|intercept|codebase|code\s+structure)",
        r"(?:understand|inspect)\s+(?:how|the)\s+(?:\w+\s+)*(?:hooks?|intercepts?|enforce\w*|routing)",
    ]
]

# Patterns indicating non-code targets — if ANY matches, allow through
# even if a code-structure pattern also matched.
_NONCODE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"(?:markdown|readme|docs?|documentation|config|yaml|yml|json|toml|ini)\b.*(?:files?|content)",
        r"(?:search|look)\s+(?:through|in)\s+(?:docs?|logs?|configs?|readme)",
        r"(?:find|search)\s+(?:all\s+)?(?:markdown|\.md|readme)\b",
        r"cross.?repo\b",
    ]
]

REASON = (
    "BLOCKED: Use cairn-graph (--location/--callers/--tests/--summary) instead of Explore agent.\n"
    "For non-code content (docs, configs, logs), use Bash directly."
)


def _is_code_structure_query(text: str) -> bool:
    """True when the prompt looks like a code-structure question the graph can answer."""
    if any(p.search(text) for p in _NONCODE_PATTERNS):
        return False
    return any(p.search(text) for p in _CODE_STRUCTURE_PATTERNS)


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
    subagent_type = tool_input.get('subagent_type', '')

    if subagent_type != 'Explore':
        sys.stdout.write('{}\n')
        return 0

    if not shutil.which('cairn-graph'):
        sys.stdout.write('{}\n')
        return 0

    prompt = tool_input.get('prompt', '')
    description = tool_input.get('description', '')
    combined = f"{description} {prompt}"

    if _is_code_structure_query(combined):
        log_event('deny_agent_explore',
                  description=description[:120],
                  prompt_head=prompt[:200])
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

    # Non-code Explore — allow through
    log_event('allow_agent_explore',
              description=description[:120],
              prompt_head=prompt[:200])
    sys.stdout.write('{}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
