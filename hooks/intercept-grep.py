#!/usr/bin/env python3
"""
Intercepts Grep tool, executes via ripgrep, caches large output.
Returns result via block (small inline, large cached).
"""

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
    GREP_THRESHOLD, CACHE_DIR
)


def find_ripgrep() -> str | None:
    """Find ripgrep binary - try Claude's bundled version first."""
    import platform

    # Determine platform-specific directory
    machine = platform.machine().lower()
    system = platform.system().lower()

    if machine in ('x86_64', 'amd64'):
        arch = 'x64'
    elif machine in ('aarch64', 'arm64'):
        arch = 'arm64'
    else:
        arch = machine

    if system == 'darwin':
        platform_dir = f'{arch}-darwin'
    elif system == 'linux':
        platform_dir = f'{arch}-linux'
    elif system == 'windows':
        platform_dir = f'{arch}-win32'
    else:
        platform_dir = None

    # Try Claude's npm package vendor directory
    if platform_dir:
        # Find claude binary and derive package path
        claude_bin = shutil.which('claude')
        if claude_bin:
            # Resolve symlink to find actual package location
            claude_path = Path(claude_bin).resolve()
            # Go up to node_modules/@anthropic-ai/claude-code
            for parent in claude_path.parents:
                vendor_rg = parent / 'vendor' / 'ripgrep' / platform_dir / 'rg'
                if vendor_rg.is_file():
                    return str(vendor_rg)

    # Try Claude's native installer version
    claude_dir = Path.home() / '.local' / 'share' / 'claude' / 'versions'
    if claude_dir.is_dir():
        versions = sorted(claude_dir.iterdir(), key=lambda p: p.name)
        if versions:
            latest = versions[-1]
            if latest.is_file() and latest.stat().st_mode & 0o111:
                return f"{latest} --ripgrep"

    # Fall back to system rg
    if shutil.which('rg'):
        return 'rg'

    return None


def run_grep(input_data: dict, path_arg: str) -> tuple[str, int]:
    """Run grep using ripgrep."""
    rg_bin = find_ripgrep()
    if not rg_bin:
        return "ripgrep not found", 1

    tool_input = input_data.get('tool_input', {})
    pattern = tool_input.get('pattern', '')
    output_mode = tool_input.get('output_mode', 'files_with_matches')
    case_insensitive = tool_input.get('-i', False)
    multiline = tool_input.get('multiline', False)
    glob = tool_input.get('glob', '')
    file_type = tool_input.get('type', '')
    context_a = tool_input.get('-A', '')
    context_b = tool_input.get('-B', '')
    context_c = tool_input.get('-C', '')
    line_numbers = tool_input.get('-n', True)
    head_limit = tool_input.get('head_limit', '')
    offset = tool_input.get('offset', '')

    # Build command
    cmd = rg_bin.split()

    # Output mode
    if output_mode == 'files_with_matches':
        cmd.append('-l')
    elif output_mode == 'count':
        cmd.append('-c')
    elif output_mode == 'content' and line_numbers:
        cmd.append('-n')

    # Options
    if case_insensitive:
        cmd.append('-i')
    if multiline:
        cmd.extend(['-U', '--multiline-dotall'])
    if glob:
        cmd.extend(['--glob', glob])
    if file_type:
        cmd.extend(['--type', file_type])
    if context_a:
        cmd.extend(['-A', str(context_a)])
    if context_b:
        cmd.extend(['-B', str(context_b)])
    if context_c:
        cmd.extend(['-C', str(context_c)])

    cmd.extend(['--', pattern, path_arg])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr

        # Apply offset and head_limit
        if output:
            lines = output.split('\n')
            if offset and int(offset) > 0:
                lines = lines[int(offset):]
            if head_limit and int(head_limit) > 0:
                lines = lines[:int(head_limit)]
            output = '\n'.join(lines)

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

    # Only handle Grep
    if tool != 'Grep':
        json_pass()
        return

    # Allow subagents through
    allow_if_subagent(transcript_path, tool_use_id)

    # Extract path
    path_arg = input_data.get('tool_input', {}).get('path', '.')

    # Resolve relative paths against cwd
    if cwd and not path_arg.startswith('/'):
        path_arg = str(Path(cwd) / path_arg)

    # Expand ~ for path checking
    check_path = str(Path(path_arg).expanduser()) if path_arg.startswith('~') else path_arg

    # Block main agent from grepping cache directories
    if '/.claude/cache/' in check_path or '/tmp/claude-tool-cache/' in check_path:
        json_block("Cache directory - use Task agent to access.", exit_code=0)
        return

    # Execute
    output, exit_code = run_grep(input_data, path_arg)
    size = len(output)
    pattern = input_data.get('tool_input', {}).get('pattern', '')

    # For grep, exit 1 = no matches (normal condition), exit 2+ = actual error
    effective_exit = 0 if exit_code <= 1 else exit_code

    # Return result (always block)
    if size <= GREP_THRESHOLD:
        log_metric("Grep", "inline", size)
        if exit_code == 0 or output:
            reason = output if output else "No matches."
        else:
            reason = "No matches."
        json_block(reason, exit_code=effective_exit)
    else:
        lines = output.count('\n')
        cache_key = cache_output_ccm(
            output,
            tool_name='Grep',
            exit_code=exit_code,
            command=f"pattern='{pattern}' path='{path_arg}'"
        )
        log_metric("Grep", "cached", size)

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
