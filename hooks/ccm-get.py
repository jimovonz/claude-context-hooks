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

from lib.ccm_cache import (
    init_ccm_cache, retrieve_content, get_metadata, get_last_key,
    list_all_keys, get_cache_stats, verify_ccm_stub
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
            'key': key[:20] + '...',
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


def main():
    parser = argparse.ArgumentParser(
        description='Retrieve content from CCM cache with REQUIRED filtering',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
At least one filter is REQUIRED. This forces intentional retrieval.

Examples:
    ccm-get.py sha256:abc123 --grep error     # Lines containing 'error'
    ccm-get.py sha256:abc123 --grep "error|warn" -C 2  # With context
    ccm-get.py sha256:abc123 --head 50        # First 50 lines
    ccm-get.py sha256:abc123 --tail 20        # Last 20 lines
    ccm-get.py sha256:abc123 --lines 100-200  # Lines 100-200
    ccm-get.py sha256:abc123 --grep "." --reason "need full file to edit"  # All content

Full retrieval (--grep ".") requires --reason explaining why filtering isn't possible.
"""
    )
    parser.add_argument('key', nargs='?', help='Cache key (sha256:...)')

    # Filtering options - at least one required
    filter_group = parser.add_argument_group('filtering (at least one required)')
    filter_group.add_argument('--grep', '-g', metavar='PATTERN',
                        help='Filter lines matching regex pattern (use "." for all, requires --reason)')
    filter_group.add_argument('--head', type=int, metavar='N',
                        help='Show first N lines')
    filter_group.add_argument('--tail', type=int, metavar='N',
                        help='Show last N lines')
    filter_group.add_argument('--lines', metavar='START-END',
                        help='Show line range (1-indexed, e.g., 100-200)')
    filter_group.add_argument('--symbol', metavar='NAME',
                        help='Extract function/class body via graph.db lookup')
    filter_group.add_argument('-C', '--context', type=int, default=0, metavar='N',
                        help='Show N lines of context around grep matches')
    filter_group.add_argument('-i', '--ignore-case', action='store_true',
                        help='Case-insensitive grep')
    filter_group.add_argument('--reason', metavar='TEXT',
                        help='Required with --grep "." - explain why full content needed (min 20 chars)')

    # Info/listing options
    parser.add_argument('--info', action='store_true',
                        help='Show metadata instead of content')
    parser.add_argument('--last', '-l', action='store_true',
                        help='Use most recently cached key')
    parser.add_argument('--list', action='store_true',
                        help='List recent cache entries')
    parser.add_argument('--stats', '-s', action='store_true',
                        help='Show cache statistics')
    parser.add_argument('--check', action='store_true',
                        help='Verify a pasted [CCM_CACHED] block is intact (transit-integrity check)')
    parser.add_argument('--limit', '-n', type=int, default=20,
                        help='Limit for --list (default: 20)')

    args = parser.parse_args()

    init_ccm_cache()

    if args.check:
        # Read a pasted [CCM_CACHED] block from stdin and verify its check
        # digit. Lets a suspect (possibly garbled-in-transit) stub be tested
        # before trusting the key it carries.
        block = sys.stdin.read()
        verdict = verify_ccm_stub(block)
        if verdict is True:
            print("OK: stub intact (check digit matches)")
        elif verdict is False:
            print("CORRUPT: check digit mismatch — stub was garbled in transit; "
                  "do not trust the key, re-run the original command", file=sys.stderr)
            sys.exit(1)
        else:
            print("UNVERIFIABLE: no check digit found (legacy stub or not a "
                  "[CCM_CACHED] block)", file=sys.stderr)
            sys.exit(1)
        return

    if args.stats:
        stats = get_cache_stats()
        print(f"Cache directory: {stats.get('cache_dir', 'unknown')}")
        print(f"Total entries: {stats.get('total_entries', 0)}")
        print(f"Total size: {stats.get('total_size_bytes', 0):,} bytes")
        print(f"Pinned entries: {stats.get('pinned_count', 0)}")
        return

    if args.list:
        keys = list_all_keys()
        if not keys:
            print("Cache is empty", file=sys.stderr)
            return

        print(f"Recent cache entries (showing {min(len(keys), args.limit)} of {len(keys)}):\n")
        for key in keys[:args.limit]:
            meta = get_metadata(key)
            if meta:
                pin_status = f" [pinned:{meta.get('pinned', {}).get('level', 'none')}]" if meta.get('pinned', {}).get('level', 'none') != 'none' else ""
                source = meta.get('source', {})
                tool = source.get('tool_name', 'unknown')
                size = meta.get('bytes_uncompressed', 0)
                print(f"  {key[:20]}...  {size:>8,} bytes  {tool}{pin_status}")
            else:
                print(f"  {key}")
        return

    # Resolve key
    key = args.key

    # Handle if user passed a path instead of just the key
    # e.g., ~/.claude/cache/b2s:abc123 -> b2s:abc123
    # or /home/user/.claude/cache/ccm/blobs/abc123.gz -> b2s:abc123
    if key and '/' in key:
        import os
        basename = os.path.basename(key)
        # Strip extension if it looks like a blob file
        if basename.endswith(('.gz', '.zst', '.txt')):
            basename = os.path.splitext(basename)[0]
        # If basename looks like a key (hex or prefixed), use it
        if basename.startswith(('b2s:', 'sha256:')) or all(c in '0123456789abcdef' for c in basename):
            if not basename.startswith(('b2s:', 'sha256:')):
                basename = f'b2s:{basename}'  # Assume b2s for raw hex
            key = basename

    if args.last:
        key = get_last_key()
        if not key:
            print("No cached items found", file=sys.stderr)
            sys.exit(1)
        if not args.key:
            pass  # Use last key
        else:
            print(f"Note: Using --last key: {key}", file=sys.stderr)

    if not key:
        parser.print_help()
        sys.exit(1)

    if args.info:
        meta = get_metadata(key)
        if not meta:
            print(f"Key not found: {key}", file=sys.stderr)
            sys.exit(1)

        print(f"Key: {meta.get('key', key)}")
        print(f"Created: {meta.get('created_at', 'unknown')}")
        print(f"Last access: {meta.get('last_access_at', 'unknown')}")
        print(f"Size: {meta.get('bytes_uncompressed', 0):,} bytes")
        print(f"Lines: {meta.get('lines', 0)}")
        print(f"Compression: {meta.get('compression', 'unknown')}")

        source = meta.get('source', {})
        if source:
            print(f"\nSource:")
            print(f"  Tool: {source.get('tool_name', 'unknown')}")
            print(f"  Exit code: {source.get('exit_code', 'unknown')}")
            if source.get('command'):
                cmd = source['command']
                if len(cmd) > 80:
                    cmd = cmd[:77] + '...'
                print(f"  Command: {cmd}")

        pinned = meta.get('pinned', {})
        if pinned.get('level', 'none') != 'none':
            print(f"\nPinned:")
            print(f"  Level: {pinned.get('level')}")
            print(f"  Reason: {pinned.get('reason', '')}")
            print(f"  Pinned at: {pinned.get('pinned_at', 'unknown')}")
        return

    # Validate: at least one filter required
    has_filter = any([args.grep, args.head, args.tail, args.lines, args.symbol])
    if not has_filter:
        print("Error: At least one filter is required.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Specify what you need:", file=sys.stderr)
        print("  --grep PATTERN   Lines matching regex", file=sys.stderr)
        print("  --head N         First N lines", file=sys.stderr)
        print("  --tail N         Last N lines", file=sys.stderr)
        print("  --lines N-M      Line range", file=sys.stderr)
        print("  --symbol NAME    Function/class body via graph.db", file=sys.stderr)
        print("", file=sys.stderr)
        print("For full content: --grep \".\" --reason \"why filtering isn't possible\"", file=sys.stderr)
        sys.exit(1)

    # Validate: --grep "." or ".*" requires --reason
    is_match_all = args.grep in ('.', '.*', '^', '.*$', '^.*$') if args.grep else False
    if is_match_all and not args.reason:
        print("Error: Full retrieval (--grep \".\") requires --reason", file=sys.stderr)
        print("", file=sys.stderr)
        print("Explain why filtering isn't possible (min 20 chars):", file=sys.stderr)
        print("  --grep \".\" --reason \"need complete file to edit multiple sections\"", file=sys.stderr)
        sys.exit(1)

    if args.reason and len(args.reason) < 20:
        print(f"Error: --reason too short ({len(args.reason)} chars, need 20+)", file=sys.stderr)
        print("", file=sys.stderr)
        print("Explain why you need full content, e.g.:", file=sys.stderr)
        print("  --reason \"editing file, need full context for changes\"", file=sys.stderr)
        sys.exit(1)

    # Get content
    content = retrieve_content(key)
    if content is None:
        print(f"Key not found or content unavailable: {key}", file=sys.stderr)
        sys.exit(1)

    lines = content.splitlines()
    original_count = len(lines)
    filtered = False

    # Warn when --lines/--head/--tail covers ~all content with no narrowing.
    # Same anti-pattern as --grep "." but via line-based bypasses — defeats
    # the stub mechanism and pulls full content back into context.
    if original_count and not args.grep and not args.symbol:
        coverage = None
        if args.head:
            coverage = min(args.head, original_count)
        elif args.tail:
            coverage = min(args.tail, original_count)
        elif args.lines:
            try:
                if '-' in args.lines:
                    _s, _e = args.lines.split('-', 1)
                    _s = int(_s) if _s else 1
                    _e = int(_e) if _e else original_count
                else:
                    _s = _e = int(args.lines)
                coverage = max(0, min(_e, original_count) - max(0, _s - 1))
            except ValueError:
                coverage = None
        if coverage is not None and coverage / original_count >= 0.9:
            pct = int(100 * coverage / original_count)
            est_tokens = original_count * 8
            print(
                f"[ccm-get: wasteful retrieval — {coverage}/{original_count} lines "
                f"({pct}%) pulls full content into context (~{est_tokens} tokens), "
                f"defeating the cache stub. Narrow with --grep PATTERN, "
                f"--symbol NAME, or a smaller --lines A-B.]",
                file=sys.stderr,
            )

    # Apply filters in order: symbol → lines range → grep → head/tail
    # This allows: --grep error --head 10 = "first 10 errors"

    # Symbol filter - resolve to line range via graph.db
    if args.symbol:
        resolved = _resolve_symbol_lines(args.symbol)
        if resolved is None:
            sys.exit(1)
        sym_start, sym_end = resolved
        lines = lines[max(0, sym_start - 1):sym_end]
        filtered = True

    # Line range filter (1-indexed) - applied first to limit search scope
    if args.lines:
        try:
            if '-' in args.lines:
                start, end = args.lines.split('-', 1)
                start = int(start) if start else 1
                end = int(end) if end else len(lines)
            else:
                start = end = int(args.lines)
            # Convert to 0-indexed
            lines = lines[max(0, start-1):end]
            filtered = True
        except ValueError:
            print(f"Invalid line range: {args.lines}", file=sys.stderr)
            sys.exit(1)

    # Grep filter - before head/tail so --head N means "first N matches"
    if args.grep:
        try:
            flags = re.IGNORECASE if args.ignore_case else 0
            pattern = re.compile(args.grep, flags)
        except re.error as e:
            print(f"Invalid regex: {e}", file=sys.stderr)
            sys.exit(1)

        if args.context > 0:
            # Grep with context
            matched_indices = set()
            for i, line in enumerate(lines):
                if pattern.search(line):
                    for j in range(max(0, i - args.context), min(len(lines), i + args.context + 1)):
                        matched_indices.add(j)

            result_lines = []
            prev_idx = -2
            for i in sorted(matched_indices):
                if prev_idx >= 0 and i > prev_idx + 1:
                    result_lines.append('--')  # Context separator
                result_lines.append(lines[i])
                prev_idx = i
            lines = result_lines
        else:
            # Simple grep
            lines = [l for l in lines if pattern.search(l)]
        filtered = True

    # Head filter - after grep, so --grep X --head N = "first N matches"
    if args.head:
        lines = lines[:args.head]
        filtered = True

    # Tail filter - after grep, so --grep X --tail N = "last N matches"
    if args.tail:
        lines = lines[-args.tail:]
        filtered = True

    # Output
    output = '\n'.join(lines)

    # Log retrieval for analysis (after filtering to capture returned size)
    log_retrieval(key, args, returned_bytes=len(output.encode('utf-8')))

    if filtered:
        print(f"[Filtered: {len(lines)} of {original_count} lines]", file=sys.stderr)
    sys.stdout.write(output)
    if output and not output.endswith('\n'):
        sys.stdout.write('\n')


if __name__ == '__main__':
    # CCH helper: refusals/errors print to stderr but must NOT exit non-zero.
    # A non-zero exit inside a parallel Bash batch makes the harness cancel
    # every sibling tool call (cascade). Real crashes (uncaught exceptions)
    # still surface naturally; argparse/sys.exit refusals are downgraded to 0.
    try:
        main()
    except SystemExit:
        pass
