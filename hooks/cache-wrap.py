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
try:
    from lib.cairn_graph_footer import generate_symbol_menu, _extract_source_file
except ImportError:
    generate_symbol_menu = None
    _extract_source_file = None

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

# Passthrough budget: a single-code-file read above the cache threshold may
# pass through UNCACHED when it is small enough and the rolling window budget
# has headroom. Full access is a finite resource, not an honor-system escape
# hatch: always-passthrough is self-defeating because it exhausts the budget
# and stubs return. Balance is printed on every passthrough so depletion is
# visible to the model. (cairn 2336462281783: gate on finite resources, not
# claims.)
PASSTHROUGH_BUDGET_TOKENS = int(os.environ.get('CCH_PASSTHROUGH_BUDGET', '25000'))
PASSTHROUGH_MAX_LINES = int(os.environ.get('CCH_PASSTHROUGH_MAX_LINES', '300'))
PASSTHROUGH_WINDOW_S = 5 * 3600
_BUDGET_FILE = Path.home() / '.claude' / 'cache' / 'passthrough_budget.json'


def _passthrough_grant(inner: str, content: str, exit_code: int) -> str | None:
    """Return a passthrough header if this read qualifies and budget allows.

    Qualifies: successful read of a single code file, ≤ PASSTHROUGH_MAX_LINES.
    Deducts from the rolling-window budget on grant.
    """
    if PASSTHROUGH_BUDGET_TOKENS <= 0 or exit_code != 0:
        return None
    if _extract_source_file is None:
        return None
    try:
        if _extract_source_file(inner, os.getcwd()) is None:
            return None
    except Exception:
        return None
    lines = content.count('\n')
    if lines > PASSTHROUGH_MAX_LINES:
        return None
    est_tokens = max(1, len(content) // 4)

    import json
    import time
    now = time.time()
    state = {'window_start': now, 'spent': 0}
    try:
        loaded = json.loads(_BUDGET_FILE.read_text())
        if now - float(loaded.get('window_start', 0)) < PASSTHROUGH_WINDOW_S:
            state = loaded
    except (OSError, ValueError):
        pass

    remaining = PASSTHROUGH_BUDGET_TOKENS - int(state.get('spent', 0))
    if est_tokens > remaining:
        return None

    state['spent'] = int(state.get('spent', 0)) + est_tokens
    try:
        _BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BUDGET_FILE.write_text(json.dumps(state))
    except OSError:
        return None  # cannot account → do not grant

    left = PASSTHROUGH_BUDGET_TOKENS - state['spent']
    resets_h = (PASSTHROUGH_WINDOW_S - (now - float(state['window_start']))) / 3600
    return (
        f'[CCM_PASSTHROUGH ~{est_tokens / 1000:.1f}k tokens · '
        f'budget {left / 1000:.1f}k/{PASSTHROUGH_BUDGET_TOKENS / 1000:.0f}k left · '
        f'window resets in {resets_h:.1f}h]'
    )


def _reported_code(inner_exit: int) -> int:
    """Exit code cache-wrap reports to the harness for an inner-command run."""
    return inner_exit if PROPAGATE_EXIT else 0



_NETWORK_FETCH_RE = None


def _maybe_convert_html(inner: str, stdout_bytes: bytes, exit_code: int) -> bytes:
    """Convert large HTML from curl/wget through cch-html.py (fail-open)."""
    global _NETWORK_FETCH_RE
    import re as _re
    if _NETWORK_FETCH_RE is None:
        _NETWORK_FETCH_RE = _re.compile(r'\b(curl|wget)\b')
    if exit_code != 0 or len(stdout_bytes) <= CACHE_THRESHOLD_BYTES:
        return stdout_bytes
    if not _NETWORK_FETCH_RE.search(inner):
        return stdout_bytes
    head = stdout_bytes[:512].lstrip().lower()
    if not (head.startswith(b'<!doctype html') or head.startswith(b'<html') or b'<html' in head):
        return stdout_bytes
    converter = Path(__file__).resolve().parent / 'cch-html.py'
    if not converter.exists():
        return stdout_bytes
    try:
        proc = subprocess.run(
            [sys.executable, str(converter)],
            input=stdout_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=20,
        )
        converted = proc.stdout or b''
        if proc.returncode == 0 and len(converted.strip()) > 0 and len(converted) < len(stdout_bytes):
            return converted
    except Exception:
        pass
    return stdout_bytes

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

    # HTML auto-convert (read-side only): network-fetch provenance + HTML
    # sniff + above threshold. Local file reads are NEVER converted — a
    # converted view would poison literal-match editing. Conversion runs
    # BEFORE the threshold check so a converted page may return inline.
    stdout_bytes = _maybe_convert_html(inner, stdout_bytes, exit_code)

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

    # Small whole-code-file read with budget headroom: pass through uncached,
    # with the balance printed so depletion is visible.
    pt_header = _passthrough_grant(inner, content, exit_code)
    if pt_header is not None:
        log_event(
            'cache_wrap',
            cmd_head=inner[:60],
            original_bytes=len(stdout_bytes),
            exit_code=exit_code,
            stub_bytes=None,
            cached=False,
            passthrough=True,
            threshold=CACHE_THRESHOLD_BYTES,
        )
        sys.stdout.write(pt_header + '\n' + content)
        if not content.endswith('\n'):
            sys.stdout.write('\n')
        if footer_line:
            sys.stdout.write(footer_line + '\n')
        sys.stdout.flush()
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
    # Symbol menu turns the stub into a retrieval menu: pick a symbol via
    # --symbol NAME instead of guessing line ranges.
    menu_line = None
    if generate_symbol_menu is not None:
        try:
            menu_line = generate_symbol_menu(inner, os.getcwd())
        except Exception:
            menu_line = None
    retrieve_hint = (
        f'Retrieve: ccm-get.py {key} '
        + ('[--symbol NAME] ' if menu_line else '')
        + '[--grep PATTERN] [--head N] [--tail N] [--lines A-B]'
    )
    # Promote cairn-graph footer + symbol menu above the stub so both are
    # visible without a retrieval round-trip.
    promoted = (footer_line + '\n' if footer_line else '') \
        + (menu_line + '\n' if menu_line else '')
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
