#!/usr/bin/env python3
"""
Intercepts Glob tool, executes via fd/find, caches large file lists.
Returns result via block (small inline, large cached).
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.common import (
    init_cache, check_passthrough, parse_hook_input, get_common_fields,
    allow_if_subagent, json_block, json_pass, cache_output_ccm, build_ccm_cache_response,
    build_retrieval_guidance, build_duplicate_stub, log_metric,
    is_key_seen, mark_key_seen, should_show_guidance, mark_guidance_shown,
    GLOB_THRESHOLD, CACHE_DIR
)


def run_glob(pattern: str, path: str) -> tuple[str, int]:
    """Run glob using fd or find."""
    # Expand ~ in path
    if path.startswith('~'):
        path = str(Path(path).expanduser())

    # Use fd if available (faster, respects gitignore)
    if shutil.which('fd'):
        # fd uses different glob syntax - strip leading **/
        fd_pattern = re.sub(r'^\*\*/', '', pattern)
        cmd = ['fd', '--type', 'f', '--glob', fd_pattern, path]
    else:
        # Use find - extract filename pattern
        if '/' in pattern:
            file_pattern = pattern.split('/')[-1]
        else:
            file_pattern = pattern
        cmd = ['find', path, '-type', 'f', '-name', file_pattern]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = result.stdout
        if not shutil.which('fd'):
            # Sort find output
            output = '\n'.join(sorted(output.strip().split('\n'))) if output.strip() else ''
        return output, result.returncode
    except subprocess.TimeoutExpired:
        return "Command timed out", 124
    except Exception as e:
        return str(e), 1


def main():
    init_cache()
    check_passthrough()

    input_data = parse_hook_input()
    tool, transcript_path, tool_use_id, cwd = get_common_fields(input_data)

    # Only handle Glob
    if tool != 'Glob':
        json_pass()
        return

    # Allow subagents through
    allow_if_subagent(transcript_path, tool_use_id)

    # Extract Glob parameters
    pattern = input_data.get('tool_input', {}).get('pattern', '')
    path_arg = input_data.get('tool_input', {}).get('path', '.')

    # Resolve relative paths against cwd
    if cwd and not path_arg.startswith('/') and not path_arg.startswith('~'):
        path_arg = str(Path(cwd) / path_arg)

    # Expand ~ for path checking
    check_path = str(Path(path_arg).expanduser()) if path_arg.startswith('~') else path_arg

    # Block main agent from globbing cache directories
    if '/.claude/cache/' in check_path or '/tmp/claude-tool-cache/' in check_path:
        json_block("Cache directory - use Task agent to access.", exit_code=0)
        return

    # Execute
    output, exit_code = run_glob(pattern, path_arg)
    size = len(output)

    # For glob/find, exit 1 = no matches (normal condition), exit 2+ = actual error
    effective_exit = 0 if exit_code <= 1 else exit_code

    # Return result (always block)
    if size <= GLOB_THRESHOLD:
        log_metric("Glob", "inline", size)
        if exit_code == 0 or output:
            reason = output if output else "No matches."
        else:
            reason = "No matches."
        json_block(reason, exit_code=effective_exit)
    else:
        lines = output.count('\n')
        cache_key = cache_output_ccm(
            output,
            tool_name='Glob',
            exit_code=exit_code,
            command=f"pattern='{pattern}' path='{path_arg}'"
        )
        log_metric("Glob", "cached", size)

        # Check if this key was seen before in this session (deduplication)
        if is_key_seen(transcript_path, cache_key):
            reason = f"{build_duplicate_stub(cache_key)}\nRetrieve: ~/.claude/hooks/ccm-get.py {cache_key}"
            json_block(reason, exit_code=effective_exit)
            return

        mark_key_seen(transcript_path, cache_key)
        reason = build_ccm_cache_response(cache_key, lines, size, exit_code, f"pattern='{pattern}' path='{path_arg}'")

        # Add retrieval guidance - verbose only on first cache in session
        verbose = should_show_guidance(transcript_path)
        guidance = build_retrieval_guidance(size, lines, verbose=verbose)
        if guidance:
            reason = reason + guidance
        if verbose:
            mark_guidance_shown(transcript_path)

        json_block(reason, exit_code=effective_exit)


if __name__ == '__main__':
    main()
