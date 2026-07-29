#!/usr/bin/env python3
"""
Retrieve content from CCM cache with REQUIRED filtering.

Filters force intentional retrieval - you must specify what you need.

Usage:
    ccm-get.py <key> --grep PATTERN       # Lines matching pattern
    ccm-get.py <key> --head N             # First N lines
    ccm-get.py <key> --tail N             # Last N lines
    ccm-get.py <key> --lines 100-200      # Line range (1-indexed)
    ccm-get.py <key> --grep error -C 3    # Matches with 3 lines context
    ccm-get.py <key> --grep "." --reason "editing file"  # Full content (requires reason)
    ccm-get.py <key> --symbol my_func     # Function body via graph.db lookup
    ccm-get.py <key> --info               # Show metadata only
"""

import json
import os
import re
import sqlite3
import sys
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib import budget
from lib.ccm_cache import (
    init_ccm_cache, retrieve_content, get_metadata, get_last_key,
    list_all_keys, get_cache_stats, verify_ccm_stub, prune_cache
)

# Retrieval log for effectiveness analysis. Stable location regardless
# of invocation path (was previously __file__.parent which split logs
# across ~/.local/bin/ and the canonical hooks dir depending on which
# symlink was invoked).
RETRIEVAL_LOG = Path.home() / '.claude' / 'cache' / 'ccm' / 'retrieval.log'


def log_retrieval(key: str, args, returned_bytes: int = None) -> None:
    """Log retrieval details for analysis."""
    try:
        RETRIEVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        meta = get_metadata(key)
        source_size = meta.get('bytes_uncompressed') if meta else None
        entry = {
            'timestamp': datetime.now().isoformat(),
            # Full key, not key[:20] + '...': the truncation made an exact
            # join against events.jsonl impossible, so every consumer had to
            # prefix-match and a naive analysis silently reported zero
            # retrievals for every cache.
            'key': key,
            'sid': os.environ.get('CCH_SESSION_ID', '')[:36],
            'filter': {
                'grep': args.grep,
                'head': args.head,
                'tail': args.tail,
                'lines': args.lines,
                'symbol': args.symbol,
                'context': args.context if args.context else None,
            },
            'reason': args.reason if args.reason else None,
            'is_full_retrieval': args.grep in ('.', '.*', '^', '.*$', '^.*$') if args.grep else False,
            'source_tool': meta.get('source', {}).get('tool_name') if meta else None,
            'source_size': source_size,
            'returned_bytes': returned_bytes,
            'savings_pct': round((1 - returned_bytes / source_size) * 100, 1) if source_size and returned_bytes else None,
        }
        # Remove None values
        entry['filter'] = {k: v for k, v in entry['filter'].items() if v is not None}

        with open(RETRIEVAL_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass  # Don't fail retrieval if logging fails


def _resolve_symbol_lines(symbol: str):
    """Resolve a symbol name to (line_start, line_end) via .code-review-graph/graph.db.

    Walks up from cwd looking for the graph database, then queries for
    Function/Class/Test nodes matching the symbol name.
    Returns (line_start, line_end) tuple or None if not found.
    """
    # Walk up from cwd to find graph.db
    d = Path(os.getcwd())
    graph_db = None
    while True:
        candidate = d / ".code-review-graph" / "graph.db"
        if candidate.is_file():
            graph_db = candidate
            break
        parent = d.parent
        if parent == d:
            break
        d = parent

    if graph_db is None:
        print("ccm-get: warning: .code-review-graph/graph.db not found", file=sys.stderr)
        return None

    try:
        conn = sqlite3.connect(str(graph_db))
        cur = conn.cursor()
        cur.execute(
            "SELECT file_path, line_start, line_end FROM nodes "
            "WHERE name = ? AND kind IN ('Function', 'Class', 'Test')",
            (symbol,),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print(f"ccm-get: warning: graph.db query failed: {e}", file=sys.stderr)
        return None

    if not rows:
        print(f"ccm-get: warning: symbol '{symbol}' not found in graph.db", file=sys.stderr)
        return None

    # Use first match (could be refined with file_path relevance heuristic)
    _file_path, line_start, line_end = rows[0]
    return (line_start, line_end)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Retrieve content from CCM cache with REQUIRED filtering',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
At least one filter is REQUIRED. This forces intentional retrieval.

Examples:
    ccm-get.py b2s:abc123 --grep error         # Lines containing 'error'
    ccm-get.py b2s:abc123 --grep "error|warn" -C 2   # With context
    ccm-get.py b2s:abc123 --head 50            # First 50 lines
    ccm-get.py b2s:abc123 --tail 20            # Last 20 lines
    ccm-get.py b2s:abc123 --lines 100-200      # Lines 100-200
    ccm-get.py b2s:abc123 --symbol handle_req  # One function body
    ccm-get.py b2s:abc123 --grep "." --reason "need full file to edit"

Full retrieval (--grep ".") spends from the shared full-content budget —
the same finite pool cache-wrap draws on for uncached passthrough. When it
is exhausted, narrow the filter or wait for the window to roll.
"""
    )
    parser.add_argument('key', nargs='?', help='Cache key (b2s:... or sha256:...)')

    filter_group = parser.add_argument_group('filtering (at least one required)')
    filter_group.add_argument('--grep', '-g', metavar='PATTERN',
                              help='Filter lines matching regex pattern (use "." for all, spends budget)')
    filter_group.add_argument('--head', type=int, metavar='N',
                              help='Show first N lines')
    filter_group.add_argument('--tail', type=int, metavar='N',
                              help='Show last N lines')
    filter_group.add_argument('--lines', metavar='START-END',
                              help='Show line range (1-indexed, e.g., 100-200)')
    filter_group.add_argument('--symbol', metavar='NAME',
                              help='Extract function/class body via graph.db lookup')
    filter_group.add_argument('--chars', metavar='START-END',
                              help='Character range (1-indexed) — for blobs whose lines are '
                                   'too long to slice by line number')
    filter_group.add_argument('-C', '--context', type=int, default=0, metavar='N',
                              help='Show N lines of context around grep matches')
    filter_group.add_argument('-i', '--ignore-case', action='store_true',
                              help='Case-insensitive grep')
    filter_group.add_argument('--reason', metavar='TEXT',
                              help='Required with --grep "." — why filtering will not do (min 20 chars)')

    parser.add_argument('--info', action='store_true',
                        help='Show metadata instead of content')
    parser.add_argument('--last', '-l', action='store_true',
                        help='Use most recently cached key')
    parser.add_argument('--list', action='store_true',
                        help='List recent cache entries')
    parser.add_argument('--stats', '-s', action='store_true',
                        help='Show cache statistics')
    parser.add_argument('--budget', action='store_true',
                        help='Show the remaining full-content budget')
    parser.add_argument('--prune', action='store_true',
                        help='Evict unpinned cache entries past the TTL / size cap')
    parser.add_argument('--ttl-days', type=int, default=None,
                        help='Override TTL for --prune')
    parser.add_argument('--max-mb', type=int, default=None,
                        help='Override size cap for --prune')
    parser.add_argument('--check', action='store_true',
                        help='Verify a pasted [CCM_CACHED] block is intact (transit-integrity check)')
    parser.add_argument('--limit', '-n', type=int, default=20,
                        help='Limit for --list (default: 20)')
    return parser


def _cmd_check() -> int:
    # Read a pasted [CCM_CACHED] block from stdin and verify its check digit.
    # Lets a suspect (possibly garbled-in-transit) stub be tested before
    # trusting the key it carries.
    verdict = verify_ccm_stub(sys.stdin.read())
    if verdict is True:
        print("OK: stub intact (check digit matches)")
        return 0
    if verdict is False:
        print("CORRUPT: check digit mismatch — stub was garbled in transit; "
              "do not trust the key, re-run the original command", file=sys.stderr)
        return 1
    print("UNVERIFIABLE: no check digit found (legacy stub or not a "
          "[CCM_CACHED] block)", file=sys.stderr)
    return 1


def _cmd_stats() -> int:
    stats = get_cache_stats()
    print(f"Cache directory: {stats.get('cache_dir', 'unknown')}")
    print(f"Total entries: {stats.get('total_entries', 0)}")
    print(f"Total size: {stats.get('total_size_bytes', 0):,} bytes")
    print(f"Pinned entries: {stats.get('pinned_count', 0)}")
    spent, total, resets_h = budget.status()
    if total:
        print(f"Full-content budget: {(total - spent) / 1000:.1f}k of "
              f"{total / 1000:.0f}k tokens left, resets in {resets_h:.1f}h")
    return 0


def _cmd_budget() -> int:
    spent, total, resets_h = budget.status()
    if not total:
        print("Full-content budget: disabled (CCH_PASSTHROUGH_BUDGET=0)")
        return 0
    print(f"Full-content budget: {(total - spent) / 1000:.1f}k of "
          f"{total / 1000:.0f}k tokens left ({spent / 1000:.1f}k spent), "
          f"window resets in {resets_h:.1f}h")
    return 0


def _cmd_prune(args) -> int:
    result = prune_cache(ttl_days=args.ttl_days, max_mb=args.max_mb)
    print(f"Pruned {result['removed']} entries "
          f"({result['freed_bytes'] / 1024 / 1024:.1f} MB freed); "
          f"{result['remaining']} remain "
          f"({result['remaining_bytes'] / 1024 / 1024:.1f} MB). "
          f"TTL {result['ttl_days']}d, cap {result['max_mb']} MB.")
    return 0


def _cmd_list(args) -> int:
    keys = list_all_keys()
    if not keys:
        print("Cache is empty", file=sys.stderr)
        return 0
    print(f"Recent cache entries (showing {min(len(keys), args.limit)} of {len(keys)}):\n")
    for key in keys[:args.limit]:
        meta = get_metadata(key)
        if not meta:
            print(f"  {key}")
            continue
        level = meta.get('pinned', {}).get('level', 'none')
        pin_status = f" [pinned:{level}]" if level != 'none' else ""
        source = meta.get('source', {})
        tool = source.get('tool_name', 'unknown')
        size = meta.get('bytes_uncompressed', 0)
        print(f"  {key[:20]}...  {size:>8,} bytes  {tool}{pin_status}")
    return 0


def _normalize_key(key: str) -> str:
    """Accept a blob path or bare hex where a key is expected."""
    if not key or '/' not in key:
        return key
    basename = os.path.basename(key)
    if basename.endswith(('.gz', '.zst', '.txt')):
        basename = os.path.splitext(basename)[0]
    if basename.startswith(('b2s:', 'sha256:')):
        return basename
    if basename and all(c in '0123456789abcdef' for c in basename):
        return f'b2s:{basename}'
    return key


def _cmd_info(key: str) -> int:
    meta = get_metadata(key)
    if not meta:
        print(f"Key not found: {key}", file=sys.stderr)
        return 1
    print(f"Key: {meta.get('key', key)}")
    print(f"Created: {meta.get('created_at', 'unknown')}")
    print(f"Last access: {meta.get('last_access_at', 'unknown')}")
    print(f"Size: {meta.get('bytes_uncompressed', 0):,} bytes")
    print(f"Lines: {meta.get('lines', 0)}")
    print(f"Compression: {meta.get('compression', 'unknown')}")
    source = meta.get('source', {})
    if source:
        print("\nSource:")
        print(f"  Tool: {source.get('tool_name', 'unknown')}")
        print(f"  Exit code: {source.get('exit_code', 'unknown')}")
        if source.get('command'):
            cmd = source['command']
            if len(cmd) > 80:
                cmd = cmd[:77] + '...'
            print(f"  Command: {cmd}")
    pinned = meta.get('pinned', {})
    if pinned.get('level', 'none') != 'none':
        print("\nPinned:")
        print(f"  Level: {pinned.get('level')}")
        print(f"  Reason: {pinned.get('reason', '')}")
        print(f"  Pinned at: {pinned.get('pinned_at', 'unknown')}")
    return 0


MATCH_ALL_PATTERNS = ('.', '.*', '^', '.*$', '^.*$')


def _require_filter(args, parser) -> None:
    if any([args.grep, args.head, args.tail, args.lines, args.symbol, args.chars]):
        return
    print("Error: At least one filter is required.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Specify what you need:", file=sys.stderr)
    print("  --grep PATTERN   Lines matching regex", file=sys.stderr)
    print("  --head N         First N lines", file=sys.stderr)
    print("  --tail N         Last N lines", file=sys.stderr)
    print("  --lines N-M      Line range", file=sys.stderr)
    print("  --symbol NAME    Function/class body via graph.db", file=sys.stderr)
    print("  --chars A-B      Character range (for very long lines)", file=sys.stderr)
    print("", file=sys.stderr)
    print("For full content: --grep \".\" --reason \"why filtering isn't possible\"",
          file=sys.stderr)
    sys.exit(1)


def _charge_full_retrieval(args, content: str) -> None:
    """Spend the shared budget for a whole-content retrieval.

    A 20-character --reason is an honor-system gate: it is satisfied without
    friction (twice in this repo's own review session) and never says no.
    The budget is the same finite, visibly-depleting pool cache-wrap uses for
    passthrough, so full retrieval now costs something real.
    """
    if args.grep not in MATCH_ALL_PATTERNS:
        return
    if not args.reason:
        print("Error: Full retrieval (--grep \".\") requires --reason", file=sys.stderr)
        print("", file=sys.stderr)
        print("Explain why filtering isn't possible (min 20 chars):", file=sys.stderr)
        print("  --grep \".\" --reason \"need complete file to edit multiple sections\"",
              file=sys.stderr)
        sys.exit(1)
    if len(args.reason) < 20:
        print(f"Error: --reason too short ({len(args.reason)} chars, need 20+)",
              file=sys.stderr)
        sys.exit(1)

    tokens = budget.estimate_tokens(content)
    granted, left, resets_h = budget.spend(tokens, kind='full_retrieval')
    if not granted:
        print(f"Error: full retrieval needs ~{tokens / 1000:.1f}k tokens but only "
              f"{left / 1000:.1f}k of the full-content budget remains "
              f"(resets in {resets_h:.1f}h).", file=sys.stderr)
        print("Narrow the filter (--symbol NAME, --grep PATTERN, --lines A-B) "
              "or wait for the window to roll.", file=sys.stderr)
        sys.exit(1)
    print(f"[budget: -{tokens / 1000:.1f}k tokens · {left / 1000:.1f}k left · "
          f"resets in {resets_h:.1f}h]", file=sys.stderr)


def _slice_chars(content: str, spec: str) -> str:
    """Character-range slice of the raw content (1-indexed, inclusive)."""
    try:
        if '-' in spec:
            start_s, end_s = spec.split('-', 1)
            start = int(start_s) if start_s else 1
            end = int(end_s) if end_s else len(content)
        else:
            start = end = int(spec)
    except ValueError:
        print(f'Invalid character range: {spec}', file=sys.stderr)
        sys.exit(1)
    return content[max(0, start - 1):end]


def _warn_wide_coverage(args, original_count: int) -> None:
    """Warn when --lines/--head/--tail covers ~all content with no narrowing.

    Same anti-pattern as --grep "." but via line-based bypasses.
    """
    if not original_count or args.grep or args.symbol:
        return
    coverage = None
    if args.head:
        coverage = min(args.head, original_count)
    elif args.tail:
        coverage = min(args.tail, original_count)
    elif args.lines:
        try:
            if '-' in args.lines:
                start_s, end_s = args.lines.split('-', 1)
                start = int(start_s) if start_s else 1
                end = int(end_s) if end_s else original_count
            else:
                start = end = int(args.lines)
            coverage = max(0, min(end, original_count) - max(1, start) + 1)
        except ValueError:
            coverage = None
    if coverage is not None and coverage >= 0.9 * original_count:
        print(f"[cch: this returns {coverage} of {original_count} lines — "
              f"filtering is not narrowing anything. Prefer --symbol NAME or a "
              f"specific --grep PATTERN.]", file=sys.stderr)


def _apply_filters(lines: list, args) -> tuple[list, bool]:
    """Apply symbol/lines/grep/head/tail in order. Returns (lines, filtered)."""
    filtered = False

    # Symbol first: it selects the region the rest of the filters work within,
    # so `--symbol X --lines 1-5` means "lines 1-5 OF X".
    if args.symbol:
        resolved = _resolve_symbol_lines(args.symbol)
        if resolved is None:
            sys.exit(1)
        sym_start, sym_end = resolved
        lines = lines[max(0, sym_start - 1):sym_end]
        filtered = True

    if args.lines:
        try:
            if '-' in args.lines:
                start_s, end_s = args.lines.split('-', 1)
                start = int(start_s) if start_s else 1
                end = int(end_s) if end_s else len(lines)
            else:
                start = end = int(args.lines)
        except ValueError:
            print(f"Invalid line range: {args.lines}", file=sys.stderr)
            sys.exit(1)
        lines = lines[max(0, start - 1):end]
        filtered = True

    # Grep before head/tail, so `--grep X --head N` means "first N matches".
    if args.grep:
        try:
            pattern = re.compile(args.grep, re.IGNORECASE if args.ignore_case else 0)
        except re.error as e:
            print(f"Invalid regex: {e}", file=sys.stderr)
            sys.exit(1)

        if args.context > 0:
            matched_indices = set()
            for i, line in enumerate(lines):
                if pattern.search(line):
                    for j in range(max(0, i - args.context),
                                   min(len(lines), i + args.context + 1)):
                        matched_indices.add(j)
            result_lines = []
            prev_idx = -2
            for i in sorted(matched_indices):
                if prev_idx >= 0 and i > prev_idx + 1:
                    result_lines.append('--')  # context separator
                result_lines.append(lines[i])
                prev_idx = i
            lines = result_lines
        else:
            lines = [line for line in lines if pattern.search(line)]
        filtered = True

    if args.head:
        lines = lines[:args.head]
        filtered = True

    if args.tail:
        lines = lines[-args.tail:]
        filtered = True

    return lines, filtered


def main():
    parser = _build_parser()
    args = parser.parse_args()

    init_ccm_cache()

    if args.check:
        return _cmd_check()
    if args.stats:
        return _cmd_stats()
    if args.budget:
        return _cmd_budget()
    if args.prune:
        return _cmd_prune(args)
    if args.list:
        return _cmd_list(args)

    key = _normalize_key(args.key)

    if args.last:
        last = get_last_key()
        if not last:
            print("No cached items found", file=sys.stderr)
            sys.exit(1)
        if args.key:
            print(f"Note: Using --last key: {last}", file=sys.stderr)
        key = last

    if not key:
        parser.print_help()
        sys.exit(1)

    if args.info:
        return _cmd_info(key)

    _require_filter(args, parser)

    content = retrieve_content(key)
    if content is None:
        print(f"Key not found or content unavailable: {key}", file=sys.stderr)
        sys.exit(1)

    _charge_full_retrieval(args, content)

    original_chars = len(content)
    char_sliced = False
    if args.chars:
        content = _slice_chars(content, args.chars)
        char_sliced = True

    lines = content.splitlines()
    original_count = len(lines)
    _warn_wide_coverage(args, original_count)

    lines, filtered = _apply_filters(lines, args)
    filtered = filtered or char_sliced

    output = '\n'.join(lines)
    log_retrieval(key, args, returned_bytes=len(output.encode('utf-8')))

    if char_sliced:
        print(f"[Filtered: {len(output)} of {original_chars} chars]", file=sys.stderr)
    elif filtered:
        print(f"[Filtered: {len(lines)} of {original_count} lines]", file=sys.stderr)
    sys.stdout.write(output)
    if output and not output.endswith('\n'):
        sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
