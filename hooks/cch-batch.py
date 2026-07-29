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

Guards: every line runs through lib/guards.py first — the same bulk-read
block, graph answer and rg -r warning the PreToolUse hook applies. Until
that was wired up, batching was a complete bypass of the guard layer
(cch-batch.py is in PASSTHROUGH_MARKERS, so the hook skips the whole
batch), which made the documented power-tool also the documented hole.

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

Same-file guard: multiple cch-edit.py / cch-write.py commands that target
the SAME file are automatically run sequentially in input order (commands
touching different files still run in parallel). Paths are compared after
symlink resolution and after honouring a `cd X && ...` prefix, so two
spellings of one file still serialize. Concurrent writers to one path
previously raced on a shared staging file; that class of corruption is
also gone at the source (lib/atomic.py stages through a unique temp
file), so this guard now only orders writes rather than preventing
corruption.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Resolve through any symlink (cch-batch is also symlinked into
# ~/.local/bin) to find cache-wrap.py in the real hooks dir.
HOOKS_DIR = Path(__file__).resolve().parent
CACHE_WRAP = HOOKS_DIR / 'cache-wrap.py'

sys.path.insert(0, str(HOOKS_DIR))

from lib import guards  # noqa: E402
from lib.event_log import log_event  # noqa: E402

_CD_PREFIX_RE = re.compile(r'^\s*cd\s+([^\s;&|]+)\s*&&\s*(.*)$')


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


def _run_plain(cmd: str, mark_exit: bool = True) -> tuple[str, int]:
    """Run a command directly via bash, bypassing cache-wrap."""
    try:
        p = subprocess.run(['bash', '-c', cmd], stdout=subprocess.PIPE,
                           stderr=None, check=False)
        out = p.stdout.decode('utf-8', 'replace')
        if mark_exit:
            if p.returncode != 0:
                out += f'\n[exit {p.returncode}]\n'
            return out, 0
        return out, p.returncode
    except Exception as e:
        return f'[cch-batch: failed: {e}]\n', 1


def target_file(cmd: str) -> str | None:
    """Real absolute path a cch-edit/cch-write line writes to, or None.

    Honours a `cd X && ...` prefix and resolves symlinks, so two spellings
    of the same file are recognised as the same serialization key.
    """
    base = os.getcwd()
    m = _CD_PREFIX_RE.match(cmd)
    if m:
        base = os.path.abspath(os.path.expanduser(m.group(1)))
        cmd = m.group(2)
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    for i, t in enumerate(toks):
        if os.path.basename(t) not in ('cch-edit.py', 'cch-write.py'):
            continue
        j = i + 1
        while j < len(toks):
            tok = toks[j]
            if tok in ('--old-file', '--new-file'):
                j += 2  # flag consumes a value
                continue
            if tok.startswith('-'):
                j += 1
                continue
            if tok in ('<<', '<', '<<<'):
                return None  # redirection reached before a path
            p = tok if os.path.isabs(tok) else os.path.join(base, tok)
            return os.path.realpath(p)
        return None
    return None


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
    parser.add_argument('--no-guards', action='store_true',
                        help='Skip the shared command guards (escape hatch; '
                             'the guards are one-shot overridable anyway)')
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

    sid = os.environ.get('CCH_SESSION_ID', '')
    cwd = os.getcwd()

    def runner(cmd: str) -> tuple[str, int]:
        # Retrieval/wrapper invocations must never be re-wrapped: caching a
        # ccm-get retrieval re-stubs content the caller just paid to retrieve
        # (recursive stubbing). Matched on the command WORD, not a substring.
        if guards.is_passthrough(cmd):
            return _run_plain(cmd)

        if not args.no_guards:
            reason = guards.block(cmd, cwd)
            if reason:
                log_event('deny_bash_guard', sid=sid, cmd_head=cmd[:120],
                          batched=True,
                          kind='graph' if 'graph:' in reason else 'bulk_read')
                return reason + '\n', 0
            cmd, applied = guards.apply_warnings(cmd)
            for w in applied:
                log_event('warn_rg_replace' if 'rg -r' in w else 'warn_bulk_sed',
                          sid=sid, cmd_head=cmd[:120], batched=True)

        if args.no_cache_wrap:
            return _run_plain(cmd, mark_exit=False)
        return _run_one(cmd)

    # Same-file guard: cch-edit/cch-write commands hitting one path are
    # chained in input order; everything else stays fully parallel.
    keys = [target_file(c) for c in commands]
    chains: list[list[int]] = []          # units of work, each run in order
    chain_of_key: dict[str, list[int]] = {}
    for idx, key in enumerate(keys):
        if key is not None and key in chain_of_key:
            chain_of_key[key].append(idx)
        else:
            unit = [idx]
            if key is not None:
                chain_of_key[key] = unit
            chains.append(unit)
    serialized = {i for unit in chains if len(unit) > 1 for i in unit}

    def run_chain(unit: list[int]) -> list[tuple[str, int]]:
        return [runner(commands[i]) for i in unit]

    jobs = max(1, min(args.jobs, len(chains)))
    results: list[tuple[str, int]] = [('', 0)] * len(commands)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for unit, unit_results in zip(chains, pool.map(run_chain, chains)):
            for i, res in zip(unit, unit_results):
                results[i] = res

    log_event('batch', sid=sid, commands=len(commands), chains=len(chains),
              serialized=len(serialized), jobs=jobs)

    n = len(commands)
    out = sys.stdout
    for i, (cmd, (text, rc)) in enumerate(zip(commands, results), 1):
        out.write(f'===[ cch-batch {i}/{n} ]=== {cmd}\n')
        if (i - 1) in serialized:
            out.write('[cch-batch: same-file guard — ran sequentially in input order]\n')
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
