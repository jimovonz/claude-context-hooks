#!/usr/bin/env python3
"""
cch-write — atomic file write through Bash routing.

Bash-routed alternative to built-in Write. Reads content from stdin
(avoiding shell quoting headaches), writes atomically via temp file +
rename, and creates parent directories on demand.

Usage:
  echo 'content' | cch-write /path/to/file
  cat source.txt | cch-write /path/to/dest
  cch-write /path/to/file < /tmp/source

  # Multi-line literal content via heredoc:
  cch-write /path/to/file << 'EOF'
  multi-line content
  with $vars and `backticks` not expanded
  EOF

Exit 0 on success, 1 on any error.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.atomic import atomic_write_bytes


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] in ('-h', '--help'):
        print('Usage: cch-write /path/to/file < content', file=sys.stderr)
        print('       echo content | cch-write /path/to/file', file=sys.stderr)
        return 1

    target = Path(sys.argv[1])
    if target.is_dir():
        print(f'cch-write: target is a directory: {target}', file=sys.stderr)
        return 1

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f'cch-write: cannot create parent directory: {e}', file=sys.stderr)
        return 1

    content = sys.stdin.buffer.read()

    try:
        real = atomic_write_bytes(target, content)
    except OSError as e:
        print(f'cch-write: write failed: {e}', file=sys.stderr)
        return 1

    print(f'cch-write: wrote {len(content)} bytes to {real}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
