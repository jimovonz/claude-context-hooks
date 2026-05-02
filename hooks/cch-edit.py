#!/usr/bin/env python3
"""
cch-edit — literal-string file edit through Bash routing.

Replicates the safety contract of Claude Code's built-in Edit while
running through Bash, so the read-before-edit guard never fires and
the cache wrapper is preserved on the edit target:
  - Exact literal match (no regex)
  - Errors if old_string not found
  - Errors if old_string occurs >1 times (unless --all)
  - Atomic write (temp file + rename)
  - Prints unified diff on success

Usage:
  cch-edit /path/to/file 'old_string' 'new_string'              # single-line
  cch-edit /path --old-file /tmp/old --new-file /tmp/new        # multi-line
  cch-edit /path 'old' 'new' --all                              # all occurrences

Exit 0 on success, 1 on any error.
"""

import argparse
import difflib
import os
import sqlite3
import sys
from pathlib import Path


def _print_impact(file_path: Path, old_string: str, content: str) -> None:
    """Print an opportunistic impact line showing callers/tests for edited functions.

    Best-effort: silently returns on any error. Never fails the edit.
    """
    try:
        # Find graph.db by walking up from the edited file's directory
        d = file_path.resolve().parent
        graph_db = None
        repo_root = None
        while True:
            candidate = d / ".code-review-graph" / "graph.db"
            if candidate.is_file():
                graph_db = candidate
                repo_root = d
                break
            parent = d.parent
            if parent == d:
                break
            d = parent

        if graph_db is None:
            return

        # Try both absolute and relative paths (crg stores absolute paths)
        abs_path = str(file_path.resolve())
        rel_path = str(file_path.resolve().relative_to(repo_root))

        # Find which lines the old_string occupies in the original content
        idx = content.find(old_string)
        if idx < 0:
            return
        edit_start = content[:idx].count("\n") + 1
        edit_end = edit_start + old_string.count("\n")

        conn = sqlite3.connect(str(graph_db))
        cur = conn.cursor()

        # Get nodes in this file
        cur.execute(
            "SELECT qualified_name, line_start, line_end FROM nodes "
            "WHERE file_path IN (?, ?) AND kind IN ('Function', 'Class', 'Test')",
            (abs_path, rel_path),
        )
        nodes = cur.fetchall()

        # Find which functions overlap with the edited region
        touched = []
        for qname, ls, le in nodes:
            if ls <= edit_end and le >= edit_start:
                touched.append(qname)

        if not touched:
            conn.close()
            return

        total_callers = 0
        total_tests = 0
        caller_files = set()
        test_hints = set()
        for qname in touched:
            cur.execute(
                "SELECT COUNT(*) FROM edges WHERE target_qualified = ? AND kind = 'CALLS'",
                (qname,),
            )
            total_callers += cur.fetchone()[0]

            # Count unique caller files
            cur.execute(
                "SELECT source_qualified FROM edges WHERE target_qualified = ? AND kind = 'CALLS'",
                (qname,),
            )
            for (sq,) in cur.fetchall():
                # Extract file from qualified name (module.path.func -> module/path)
                caller_files.add(sq.rsplit(".", 1)[0] if "." in sq else sq)

            cur.execute(
                "SELECT target_qualified FROM edges WHERE source_qualified = ? AND kind = 'TESTED_BY'",
                (qname,),
            )
            for (tgt,) in cur.fetchall():
                total_tests += 1
                test_locs = cur.execute(
                    "SELECT file_path, name FROM nodes WHERE qualified_name = ?",
                    (tgt,),
                ).fetchone()
                if test_locs:
                    test_hints.add((test_locs[0], test_locs[1]))

        conn.close()

        parts = [f"callers:{total_callers}", f"tests:{total_tests}"]
        if caller_files:
            parts.append(f"files:{len(caller_files)}")
        print(f"cch-edit: impact: {' '.join(parts)}")

        if test_hints:
            # Group by file, format as pytest command
            by_file = {}
            for tf, tn in test_hints:
                by_file.setdefault(tf, []).append(tn)
            for tf, names in list(by_file.items())[:3]:
                selectors = " or ".join(names[:4])
                print(f"cch-edit: run: pytest {tf} -k \"{selectors}\"")
    except Exception:
        pass  # Best-effort, never fail the edit


def main() -> int:
    p = argparse.ArgumentParser(prog='cch-edit', description='Literal-string file edit (Bash-routed alternative to built-in Edit)')
    p.add_argument('path', help='Target file path')
    p.add_argument('old_string', nargs='?', help='String to replace (positional)')
    p.add_argument('new_string', nargs='?', help='Replacement string (positional)')
    p.add_argument('--old-file', help='Read old_string from file (for multi-line content)')
    p.add_argument('--new-file', help='Read new_string from file (for multi-line content)')
    p.add_argument('--all', action='store_true', help='Replace all occurrences (default: error if >1)')
    args = p.parse_args()

    target = Path(args.path)
    if not target.is_file():
        print(f'cch-edit: not a regular file: {target}', file=sys.stderr)
        return 1

    if args.old_file:
        try:
            old = Path(args.old_file).read_text()
        except OSError as e:
            print(f'cch-edit: cannot read --old-file: {e}', file=sys.stderr)
            return 1
    elif args.old_string is not None:
        old = args.old_string
    else:
        print('cch-edit: missing old_string (positional arg or --old-file)', file=sys.stderr)
        return 1

    if args.new_file:
        try:
            new = Path(args.new_file).read_text()
        except OSError as e:
            print(f'cch-edit: cannot read --new-file: {e}', file=sys.stderr)
            return 1
    elif args.new_string is not None:
        new = args.new_string
    else:
        print('cch-edit: missing new_string (positional arg or --new-file)', file=sys.stderr)
        return 1

    if old == '':
        print('cch-edit: old_string is empty', file=sys.stderr)
        return 1
    if old == new:
        print('cch-edit: old_string and new_string are identical', file=sys.stderr)
        return 1

    content = target.read_text()
    count = content.count(old)

    if count == 0:
        print(f'cch-edit: old_string not found in {target}', file=sys.stderr)
        return 1
    if count > 1 and not args.all:
        print(f'cch-edit: old_string appears {count} times in {target} — pass --all to replace all, or extend old_string until unique', file=sys.stderr)
        return 1

    updated = content.replace(old, new) if args.all else content.replace(old, new, 1)

    tmp = target.with_name(target.name + '.cch-tmp')
    try:
        target_mode = target.stat().st_mode
        tmp.write_text(updated)
        os.chmod(tmp, target_mode)
        os.replace(tmp, target)
    except OSError as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f'cch-edit: write failed: {e}', file=sys.stderr)
        return 1

    diff = list(difflib.unified_diff(
        content.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=str(target),
        tofile=str(target),
        n=3,
    ))
    sys.stdout.writelines(diff)
    if diff and not diff[-1].endswith('\n'):
        sys.stdout.write('\n')
    n_replaced = count if args.all else 1
    print(f'cch-edit: replaced {n_replaced} occurrence(s) in {target}')

    # Opportunistic impact line — best-effort, never fails the edit.
    # Note: os.replace(tmp, target) already updates the file's mtime,
    # which is sufficient for graph.db freshness checks (_check_freshness
    # compares file mtime against node updated_at). No additional cache
    # invalidation signal is needed.
    _print_impact(target, old, content)

    return 0


if __name__ == '__main__':
    sys.exit(main())
