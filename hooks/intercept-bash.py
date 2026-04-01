#!/usr/bin/env python3
"""
Intercepts Bash tool to:
1. Block main agent from accessing cache directly
2. Execute non-trivial commands, cache large output
3. Return all results via block (no double-execution)
4. Allow subagents through unimpeded
"""

import re
import sys
from pathlib import Path

# Add hooks dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from lib.common import (
    init_cache, check_passthrough, parse_hook_input, get_common_fields,
    allow_if_subagent, json_block, json_pass, cache_output_ccm, build_ccm_cache_response,
    build_retrieval_guidance, build_duplicate_stub, log_metric, run_command, probe_command,
    get_command_classification, is_cached_interactive, learn_command_classification,
    is_key_seen, mark_key_seen, should_show_guidance, mark_guidance_shown,
    BASH_THRESHOLD, CACHE_DIR
)


def is_ccm_script(cmd: str) -> bool:
    """Check if command runs our own CCM management scripts."""
    ccm_patterns = [
        r'claude-session-purge\.py',
        r'claude-cache-prune\.py',
        r'context-monitor\.py',
        r'intercept-.*\.py',
        r'learn-large-commands\.py',
        r'pre-compact\.py',
        r'ccm-get\.py',
        r'~/.claude/hooks/',
        r'\.claude/hooks/',
    ]
    for pattern in ccm_patterns:
        if re.search(pattern, cmd):
            return True
    return False


def is_obviously_small(cmd: str) -> bool:
    """Check if command is trivially small output."""
    # Compound commands need execution to measure
    if re.search(r'(&&|;|\|)', cmd):
        return False

    small_patterns = [
        r'^ls$', r'^ls\s+-[alh]', r'^pwd$', r'^whoami$', r'^id$', r'^date$',
        r'^echo\s+', r'^printf\s+',
        r'^cd\s+', r'^mkdir\s+', r'^touch\s+', r'^rm\s+', r'^mv\s+', r'^cp\s+',
        r'^git\s+status$', r'^git\s+branch', r'^git\s+remote',
        r'^git\s+log\s+-\d+', r'^git\s+log\s+--oneline\s+-\d+',
        r'^which\s+', r'^type\s+', r'^command\s+-v\s+', r'^hash\s+',
        r'^head\s+', r'^tail\s+', r'^wc\s+', r'^stat\s+',
        r'^test\s+', r'^\[\s+',
        r'^[A-Za-z_][A-Za-z0-9_]*=',  # Variable assignments
    ]

    for pattern in small_patterns:
        if re.match(pattern, cmd):
            return True
    return False


def is_obviously_interactive(cmd: str) -> bool:
    """Check if command is obviously interactive (TTY-based)."""
    interactive_patterns = [
        r'^(vim|vi|nano|emacs|less|more|man|top|htop|btop|watch)\s*',
        r'^(ssh|telnet|ftp|sftp)\s+',
        r'^(python|python3|node|ruby|irb|ghci)$',
    ]

    for pattern in interactive_patterns:
        if re.match(pattern, cmd):
            return True

    # Check for -i flag
    if re.search(r'(^|\s)-i(\s|$)', cmd):
        return True

    # Check cache for known interactive commands
    cached = is_cached_interactive(cmd)
    if cached is True:
        return True

    return False


def classify_unknown_command(cmd: str) -> dict:
    """Classify unknown command using Haiku or probe."""
    # Try Haiku classification (cached or fresh)
    classification = get_command_classification(cmd)
    if classification:
        return classification

    # Fallback: assume non-interactive, small output
    return {'interactive': 0, 'large_output': 0}


def main():
    init_cache()
    check_passthrough()

    input_data = parse_hook_input()
    tool, transcript_path, tool_use_id, cwd = get_common_fields(input_data)

    # Only handle Bash
    if tool != 'Bash':
        json_pass()
        return

    cmd = input_data.get('tool_input', {}).get('command', '')
    timeout_ms = input_data.get('tool_input', {}).get('timeout', 120000)

    # Allow subagents through
    allow_if_subagent(transcript_path, tool_use_id)

    # CCM management scripts: always pass through
    if is_ccm_script(cmd):
        log_metric("Bash", "ccm-passthrough", 0)
        json_pass()
        return

    # Cache access blocking (simple string match - more reliable than regex)
    if '/.claude/cache/' in cmd or '/tmp/claude-tool-cache/' in cmd:
        # Allow listing/cleaning/stat commands
        if re.match(r'^(ls|rm|find|wc|stat|du|df|/bin/ls|/usr/bin/find|/bin/rm)(\s|$)', cmd):
            json_pass()
            return
        json_block("Cache file - use Task agent to read.", exit_code=0)
        return

    # Trivial commands: let Claude handle natively
    if is_obviously_small(cmd):
        log_metric("Bash", "pass", 0)
        json_pass()
        return

    # Interactive commands: let Claude handle
    if is_obviously_interactive(cmd):
        log_metric("Bash", "interactive", 0)
        json_pass()
        return

    # For unknown commands, classify with Haiku before running
    classification = classify_unknown_command(cmd)

    if classification.get('interactive', 0) == 1:
        log_metric("Bash", "haiku-interactive", 0)
        json_pass()
        return

    # Execute with probe as backup detection
    timeout_sec = min(max(timeout_ms // 1000, 1), 600)
    output, exit_code, is_interactive = probe_command(cmd, cwd, timeout_sec)

    # If probe detected as interactive (Haiku was wrong), learn and pass through
    if is_interactive:
        learn_command_classification(cmd, interactive=True)
        log_metric("Bash", "probe-interactive", 0)
        json_pass()
        return

    size = len(output)

    # Learn if output was large
    if size > BASH_THRESHOLD:
        learn_command_classification(cmd, large_output=True)

    # Return result (always block to avoid double-execution)
    if size <= BASH_THRESHOLD:
        log_metric("Bash", "inline", size)
        reason = f"Exit {exit_code}:\n\n{output}"
        json_block(reason, exit_code=exit_code)
    else:
        lines = output.count('\n')
        cache_key = cache_output_ccm(
            output,
            tool_name='Bash',
            exit_code=exit_code,
            command=cmd,
            cwd=cwd
        )
        log_metric("Bash", "cached", size)

        # Check if this key was seen before in this session (deduplication)
        if is_key_seen(transcript_path, cache_key):
            reason = f"{build_duplicate_stub(cache_key)}\nRetrieve: ~/.claude/hooks/ccm-get.py {cache_key}"
            json_block(reason, exit_code=exit_code)
            return

        mark_key_seen(transcript_path, cache_key)
        reason = build_ccm_cache_response(cache_key, lines, size, exit_code, cmd)

        # Add retrieval guidance - verbose only on first cache in session
        verbose = should_show_guidance(transcript_path)
        guidance = build_retrieval_guidance(size, lines, verbose=verbose)
        if guidance:
            reason = reason + guidance
        if verbose:
            mark_guidance_shown(transcript_path)

        json_block(reason, exit_code=exit_code)


if __name__ == '__main__':
    main()
