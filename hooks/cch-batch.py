#!/usr/bin/env python3
"""
cch-batch — run many Bash commands concurrently in ONE tool call.

Why this exists: the Claude Code harness cancels every sibling tool call
in a parallel batch if any one exits non-zero (cairn 2336462277017). The
fail-soft channel split in cache-wrap.py already neutralizes that for
ad-hoc parallel calls, but cch-batch is the deliberate power-tool: it
collapses N independent commands into a SINGLE tool call, so there are no
siblings for the harness to cancel — cascade-immune by construction — and
it runs them concurrently for real wall-clock parallelism.

Each command is shelled through cache-wrap.py, so it inherits everything:
fail-soft exit handling, RTK compression (already applied upstream of the
inner command), large-output caching to its own CCM key, and the stub
check digit. Large outputs become per-command [CCM_CACHED] stubs you can
ccm-get individually; small outputs appear inline.

Usage:
  # one command per line on stdin
  cch-batch.py << 'EOF'
  rg -n TODO src/
  fd -e py tests/
  git log --oneline -5
  EOF

  # from a file, capped concurrency
  cch-batch.py --jobs 4 < /tmp/cmds.txt

  # blank lines and #-comments are ignored

Output: one delimited block per command, in input order, each labelled
with its index and command. cch-batch itself always exits 0 (the whole
point is that nothing it runs can cancel anything).
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Resolve through any symlink (cch-batch is also symlinked into
# ~/.local/bin) to find cache-wrap.py in the real hooks dir.
CACHE_WRAP = Path(__file__).resolve().parent / 'cache-wrap.py'


def _run_one(cmd: str) -> tuple[str, int]:
    """Run a single command through cache-wrap; return (stdout, reported_rc).

    stderr streams through to the caller's stderr directly. cache-wrap is
    fail-soft so reported_rc is normally 0; the real inner exit code rides
    in-band ([exit N] / stub exit:) within stdout.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(CACHE_WRAP), '--', cmd],
            stdout=subprocess.PIPE,
            stderr=None,
            check=False,
        )
        return proc.stdout.decode('utf-8', 'replace'), proc.returncode
    except Exception as e:  # never let one command sink the batch
        return f'[cch-batch: failed to run command: {e}]\n', 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Run many Bash commands concurrently in one tool call.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--jobs', '-j', type=int, default=8,
                        help='Max concurrent commands (default: 8)')
    parser.add_argument('--no-cache-wrap', action='store_true',
                        help='Run commands via bash -c directly, bypassing '
                             'cache-wrap (no per-command caching/fail-soft)')
    args = parser.parse_args()

    # Parse commands: one per line, skip blanks and #-comments.
    commands = []
    for line in sys.stdin.read().splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        commands.append(s)

    if not commands:
        sys.stderr.write('cch-batch: no commands on stdin\n')
        return 0

    def runner(cmd: str) -> tuple[str, int]:
        if args.no_cache_wrap:
            try:
                p = subprocess.run(['bash', '-c', cmd], stdout=subprocess.PIPE,
                                   stderr=None, check=False)
                return p.stdout.decode('utf-8', 'replace'), p.returncode
            except Exception as e:
                return f'[cch-batch: failed: {e}]\n', 1
        return _run_one(cmd)

    # Run concurrently, preserve input order in output.
    jobs = max(1, min(args.jobs, len(commands)))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(runner, commands))

    n = len(commands)
    out = sys.stdout
    for i, (cmd, (text, rc)) in enumerate(zip(commands, results), 1):
        out.write(f'===[ cch-batch {i}/{n} ]=== {cmd}\n')
        out.write(text)
        if text and not text.endswith('\n'):
            out.write('\n')
        # Surface a real failure marker only when cache-wrap propagated a
        # raw non-zero (e.g. --no-cache-wrap mode); in fail-soft mode the
        # [exit N] is already inside `text`.
        if rc != 0 and args.no_cache_wrap:
            out.write(f'[exit {rc}]\n')
        out.write('\n')
    out.flush()

    # cch-batch is always fail-soft: one tool call, nothing to cancel.
    return 0


if __name__ == '__main__':
    sys.exit(main())
