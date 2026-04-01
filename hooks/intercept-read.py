#!/usr/bin/env python3
"""
Intercepts Read tool for large files, caches and returns reference.
Small files and paginated reads pass through to Claude.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.common import (
    init_cache, check_passthrough, parse_hook_input, get_common_fields,
    allow_if_subagent, json_block, json_pass, cache_output_ccm, build_ccm_cache_response,
    build_retrieval_guidance, build_duplicate_stub, log_metric, READ_THRESHOLD,
    is_key_seen, mark_key_seen, should_show_guidance, mark_guidance_shown
)

# File patterns that should never be intercepted (full content required)
PASSTHROUGH_PATTERNS = re.compile(
    r'^CLAUDE\.md$|^README\.md$|^README$|\.json$|\.yaml$|\.yml$|\.toml$|\.lock$|\.env',
    re.IGNORECASE
)


def main():
    init_cache()
    check_passthrough()

    input_data = parse_hook_input()
    tool, transcript_path, tool_use_id, cwd = get_common_fields(input_data)

    # Only handle Read
    if tool != 'Read':
        json_pass()
        return

    # Allow subagents through
    allow_if_subagent(transcript_path, tool_use_id)

    # Extract file path
    tool_input = input_data.get('tool_input', {})
    file_path = tool_input.get('file_path', '')

    # Expand ~ first before any path resolution
    if file_path.startswith('~'):
        file_path = str(Path(file_path).expanduser())

    # Resolve relative paths against cwd
    if cwd and not file_path.startswith('/'):
        file_path = str(Path(cwd) / file_path)

    file_path = Path(file_path)

    # Block main agent from reading cache files
    file_path_str = str(file_path)
    if '/tmp/claude-tool-cache/' in file_path_str:
        json_block("Cache file - use Task agent or ccm-get.py to retrieve.", exit_code=0)
        return
    if '/.claude/cache/' in file_path_str:
        # Check if this is a CCM blob path (contains sha256 hash)
        ccm_match = re.search(r'ccm/blobs/([a-f0-9]{2})/([a-f0-9]{62})', file_path_str)
        if ccm_match:
            key = f"sha256:{ccm_match.group(1)}{ccm_match.group(2)}"
            json_block(f"CCM cached content. Retrieve with: ~/.claude/hooks/ccm-get.py {key}", exit_code=0)
            return
        # Legacy cache file (8-char hex)
        legacy_match = re.search(r'/cache/([a-f0-9]{8})$', file_path_str)
        if legacy_match:
            json_block(f"Cached content. Retrieve with: ~/.claude/hooks/ccm-get.py ~/.claude/cache/{legacy_match.group(1)}", exit_code=0)
            return
        # Generic cache path
        json_block("Cache file - use Task agent or ~/.claude/hooks/ccm-get.py to retrieve.", exit_code=0)
        return

    # Extract remaining Read parameters
    offset = tool_input.get('offset')
    limit = tool_input.get('limit')

    # If offset/limit specified, user is already paginating - let it through
    if offset is not None or limit is not None:
        log_metric("Read", "paginated", 0)
        json_pass()
        return

    # Check file exists
    if not file_path.is_file():
        log_metric("Read", "notfound", 0)
        json_pass()
        return

    # File whitelist: never intercept these
    if PASSTHROUGH_PATTERNS.search(file_path.name):
        log_metric("Read", "whitelist", 0)
        json_pass()
        return

    # Check file size
    file_size = file_path.stat().st_size

    # Small files pass through
    if file_size < READ_THRESHOLD:
        log_metric("Read", "pass", file_size)
        json_pass()
        return

    # Large file - cache it and return reference
    try:
        content = file_path.read_text()
    except Exception as e:
        json_block(f"Error reading file: {e}")
        return

    lines = content.count('\n')
    cache_key = cache_output_ccm(
        content,
        tool_name='Read',
        exit_code=0,
        command=str(file_path)
    )
    log_metric("Read", "cached", file_size)

    # Check if this key was seen before in this session (deduplication)
    if is_key_seen(transcript_path, cache_key):
        # Return minimal duplicate stub
        reason = f"{build_duplicate_stub(cache_key)}\nRetrieve: ~/.claude/hooks/ccm-get.py {cache_key}"
        json_block(reason, exit_code=0)
        return

    # Mark key as seen
    mark_key_seen(transcript_path, cache_key)

    reason = build_ccm_cache_response(cache_key, lines, file_size, 0, str(file_path))

    # Add retrieval guidance - verbose only on first cache in session
    verbose = should_show_guidance(transcript_path)
    guidance = build_retrieval_guidance(file_size, lines, verbose=verbose)
    if guidance:
        reason = reason + guidance

    # Mark guidance as shown after first cache
    if verbose:
        mark_guidance_shown(transcript_path)

    json_block(reason, exit_code=0)


if __name__ == '__main__':
    main()
