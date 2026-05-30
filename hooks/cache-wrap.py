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

# Fail-soft channel split. An inner command's exit code serves two readers:
# the harness control-flow (which cancels sibling tool calls in a parallel
# batch when any one "errors") and the human/model (who needs to know if the
# work succeeded). The harness welds the cascade decision to that one integer,
# so a benign non-zero exit (grep no-match, ls missing, diff differs, pkill
# self-match → 144) cancels every unrelated sibling call in the same turn.
#
# We split the channel: report 0 to the harness so siblings never get
# cancelled, and carry the REAL exit code in-band where the model can see it
# ([exit N] marker on the inline path; the stub's `exit:` field on the cached
# path). Shell-internal semantics (&&, ||, set -e) already resolved INSIDE the
# `bash -c` subprocess before we report, so neutralizing the OUTER code changes
# only the harness's cascade decision — nothing else.
#
# Set CCH_PROPAGATE_EXIT=1 to restore raw propagation (for automation that
# genuinely depends on cache-wrap's process exit code). Wrapper-usage errors
# (bad argv, bash-not-found) always propagate regardless — those are "you
# invoked the tool wrong", not inner-command results.
PROPAGATE_EXIT = os.environ.get('CCH_PROPAGATE_EXIT') == '1'


def _reported_code(inner_exit: int) -> int:
    """Exit code cache-wrap reports to the harness for an inner-command run."""
    return inner_exit if PROPAGATE_EXIT else 0


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
            exit_code=exit_code,
            stub_bytes=None,
            cached=False,
            threshold=CACHE_THRESHOLD_BYTES,
        )
        sys.stdout.buffer.write(stdout_bytes)
        if footer_line:
            if stdout_bytes and not stdout_bytes.endswith(b'\n'):
                sys.stdout.buffer.write(b'\n')
            sys.stdout.buffer.write(footer_line.encode('utf-8') + b'\n')
        # In-band exit marker so a non-zero result stays legible even though
        # we report 0 to the harness (fail-soft channel split). Skipped when
        # propagating raw, since the process exit code already carries it.
        if exit_code != 0 and not PROPAGATE_EXIT:
            if stdout_bytes and not stdout_bytes.endswith(b'\n'):
                sys.stdout.buffer.write(b'\n')
            sys.stdout.buffer.write(f'[exit {exit_code}]\n'.encode('utf-8'))
        sys.stdout.flush()
        return _reported_code(exit_code)

    # Above threshold: cache and emit stub.
    try:
        content = stdout_bytes.decode('utf-8')
    except UnicodeDecodeError:
        # Binary-ish output — don't cache, pass through. Caching binary
        # in a text-oriented cache would corrupt it.
        sys.stdout.buffer.write(stdout_bytes)
        sys.stdout.flush()
        # Can't append a text marker to binary stdout without corrupting it,
        # so surface the real exit code on stderr instead (still fail-soft:
        # we report 0 to the harness via _reported_code).
        if exit_code != 0 and not PROPAGATE_EXIT:
            sys.stderr.write(f'[exit {exit_code}]\n')
        return _reported_code(exit_code)

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
        exit_code=exit_code,
        stub_bytes=len(full_emit.encode('utf-8')),
        cached=True,
        cache_key=key,
        threshold=CACHE_THRESHOLD_BYTES,
    )
    sys.stdout.write(full_emit)
    sys.stdout.flush()
    # Cached path: the stub's `exit:` field already carries the real code
    # in-band, so just neutralize the reported code (fail-soft channel split).
    return _reported_code(exit_code)


if __name__ == '__main__':
    sys.exit(main())
