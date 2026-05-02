#!/usr/bin/env python3
"""
Bash output cache wrapper.

Runs the inner command, captures its stdout, measures size after any
upstream RTK rewrite has already taken effect. Below threshold: emits
output unchanged. Above threshold: writes to content-addressable cache
and emits a [CCM_CACHED] stub on stdout.

Invoked by intercept-bash.py via updatedInput rewrite:
    cache-wrap.py -- <inner-command-string>

The inner command runs through `bash -c` so all shell features (pipes,
redirects, env, &&, etc.) work unchanged. stderr is passed through to
the parent (Bash tool merges it with stdout in tool_result), but is NOT
included in the size measurement — only stdout is cached.

Exit code propagates from the inner command.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.ccm_cache import init_ccm_cache, store_content, build_ccm_stub
from lib.event_log import log_event
try:
    from lib.cairn_graph_footer import generate_footer
except ImportError:
    generate_footer = None

# Threshold for caching. After RTK compression typical Bash output is
# small; raise this if it caches too eagerly.
CACHE_THRESHOLD_BYTES = int(os.environ.get('CCH_CACHE_THRESHOLD', '8000'))


def main() -> int:
    # Argv: cache-wrap.py -- <inner command...>
    if len(sys.argv) < 3 or sys.argv[1] != '--':
        sys.stderr.write('cache-wrap: usage: cache-wrap.py -- <command>\n')
        return 2

    # The inner command is everything after '--', joined back into one
    # bash -c argument. intercept-bash.py passes a single shell-quoted
    # argument so sys.argv[2] is normally the whole string.
    inner = ' '.join(sys.argv[2:])

    init_ccm_cache()

    # Run inner via bash -c so shell features work. stdout is captured
    # for measurement; stderr streams through directly.
    try:
        proc = subprocess.run(
            ['bash', '-c', inner],
            stdout=subprocess.PIPE,
            stderr=None,
            check=False,
        )
    except FileNotFoundError:
        sys.stderr.write('cache-wrap: bash not found on PATH\n')
        return 127

    stdout_bytes = proc.stdout or b''
    exit_code = proc.returncode

    # Generate cairn-graph footer for code-file reads (best-effort)
    footer_line = None
    if exit_code == 0 and stdout_bytes and generate_footer is not None:
        try:
            footer_line = generate_footer(inner, os.getcwd())
        except Exception:
            pass

    if len(stdout_bytes) <= CACHE_THRESHOLD_BYTES:
        # Inline: write through unchanged.
        log_event(
            'cache_wrap',
            cmd_head=inner[:60],
            original_bytes=len(stdout_bytes),
            stub_bytes=None,
            cached=False,
            threshold=CACHE_THRESHOLD_BYTES,
        )
        sys.stdout.buffer.write(stdout_bytes)
        if footer_line:
            if stdout_bytes and not stdout_bytes.endswith(b'\n'):
                sys.stdout.buffer.write(b'\n')
            sys.stdout.buffer.write(footer_line.encode('utf-8') + b'\n')
        sys.stdout.flush()
        return exit_code

    # Above threshold: cache and emit stub.
    try:
        content = stdout_bytes.decode('utf-8')
    except UnicodeDecodeError:
        # Binary-ish output — don't cache, pass through. Caching binary
        # in a text-oriented cache would corrupt it.
        sys.stdout.buffer.write(stdout_bytes)
        sys.stdout.flush()
        return exit_code

    # Append footer to cached content so it appears in ccm-get retrieval
    if footer_line:
        content = content.rstrip('\n') + '\n' + footer_line + '\n'

    key = store_content(
        content,
        source={
            'tool_name': 'Bash',
            'command': inner[:200],
            'exit_code': exit_code,
        },
    )
    lines = content.count('\n')
    stub = build_ccm_stub(
        key=key,
        bytes_uncompressed=len(stdout_bytes),
        lines=lines,
        exit_code=exit_code,
        tool_name='Bash',
        command=inner,
    )
    retrieve_hint = (
        f'Retrieve: ccm-get.py {key} '
        '[--grep PATTERN] [--head N] [--tail N] [--lines A-B]'
    )
    # Promote cairn-graph footer above stub so it's visible without ccm-get
    promoted = footer_line + '\n' if footer_line else ''
    full_emit = promoted + stub + '\n' + retrieve_hint + '\n'
    log_event(
        'cache_wrap',
        cmd_head=inner[:60],
        original_bytes=len(stdout_bytes),
        stub_bytes=len(full_emit.encode('utf-8')),
        cached=True,
        cache_key=key,
        threshold=CACHE_THRESHOLD_BYTES,
    )
    sys.stdout.write(full_emit)
    sys.stdout.flush()
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
