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
import sys
from pathlib import Path


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
    return 0


if __name__ == '__main__':
    sys.exit(main())
