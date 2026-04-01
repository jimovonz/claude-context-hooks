#!/usr/bin/env python3
"""
Intercepts WebFetch tool for large web content, caches and returns reference.
Small responses pass through to Claude.
"""

import sys
import urllib.request
import urllib.error
import ssl
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.common import (
    init_cache, check_passthrough, parse_hook_input, get_common_fields,
    allow_if_subagent, json_block, json_pass, cache_output_ccm, build_ccm_cache_response,
    build_retrieval_guidance, build_duplicate_stub, log_metric,
    is_key_seen, mark_key_seen, should_show_guidance, mark_guidance_shown
)

# Threshold for caching web content (bytes)
WEBFETCH_THRESHOLD = 4000  # ~1k tokens


def fetch_url(url: str, timeout: int = 30) -> tuple[str, int, str]:
    """
    Fetch URL content.
    Returns: (content, status_code, error_message)
    """
    try:
        # Create SSL context that doesn't verify (some sites have cert issues)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; ClaudeBot/1.0)',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }
        )

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            content = response.read()
            # Try to decode as text
            charset = response.headers.get_content_charset() or 'utf-8'
            try:
                text = content.decode(charset)
            except (UnicodeDecodeError, LookupError):
                try:
                    text = content.decode('utf-8', errors='replace')
                except:
                    text = content.decode('latin-1')
            return text, response.status, ''

    except urllib.error.HTTPError as e:
        return '', e.code, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return '', 0, f"URL Error: {e.reason}"
    except Exception as e:
        return '', 0, f"Error: {str(e)}"


def main():
    init_cache()
    check_passthrough()

    input_data = parse_hook_input()
    tool, transcript_path, tool_use_id, cwd = get_common_fields(input_data)

    # Only handle WebFetch
    if tool != 'WebFetch':
        json_pass()
        return

    # Allow subagents through (they need full content for processing)
    allow_if_subagent(transcript_path, tool_use_id)

    # Extract parameters
    tool_input = input_data.get('tool_input', {})
    url = tool_input.get('url', '')
    prompt = tool_input.get('prompt', '')

    if not url:
        json_pass()
        return

    # Fetch the content
    content, status_code, error = fetch_url(url)

    if error:
        log_metric("WebFetch", "error", 0)
        json_block(f"WebFetch failed: {error}", exit_code=1)
        return

    content_size = len(content.encode('utf-8'))
    lines = content.count('\n')

    # Small content passes through (let Claude's WebFetch handle it with the prompt)
    if content_size < WEBFETCH_THRESHOLD:
        log_metric("WebFetch", "pass", content_size)
        json_pass()
        return

    # Large content - cache it and return reference
    cache_key = cache_output_ccm(
        content,
        tool_name='WebFetch',
        exit_code=0,
        command=url[:100]  # Truncate long URLs
    )
    log_metric("WebFetch", "cached", content_size)

    # Check if this key was seen before in this session (deduplication)
    if is_key_seen(transcript_path, cache_key):
        reason = f"{build_duplicate_stub(cache_key)}\nRetrieve: ~/.claude/hooks/ccm-get.py {cache_key}"
        json_block(reason, exit_code=0)
        return

    # Mark key as seen
    mark_key_seen(transcript_path, cache_key)

    # Build response with context about the original request
    reason = build_ccm_cache_response(cache_key, lines, content_size, 0, url[:80])

    # Add note about the prompt that was requested
    if prompt:
        reason += f"\n\nOriginal prompt: \"{prompt[:100]}{'...' if len(prompt) > 100 else ''}\""
        reason += "\nUse Task agent with ccm-get.py to process cached content with this prompt."

    # Add retrieval guidance
    verbose = should_show_guidance(transcript_path)
    guidance = build_retrieval_guidance(content_size, lines, verbose=verbose)
    if guidance:
        reason = reason + guidance

    if verbose:
        mark_guidance_shown(transcript_path)

    json_block(reason, exit_code=0)


if __name__ == '__main__':
    main()
