"""Delta emission — stop re-sending output the session has already seen.

The cache is content-addressed, so byte-identical output already dedupes on
disk. It was still re-emitted to the model in full every single time: the
verify-after-edit loop, repeated `git status`, re-reading a file whose content
did not change — each one paid full price on every repeat.

Two shapes, keyed by (session, command):

  * identical output -> a one-line [CCM_UNCHANGED] header naming the cache
    key. The content stays retrievable with ccm-get, so nothing is lost if
    the earlier copy has since been compacted out of context.

  * changed output -> a unified diff against the previous emission, used
    only when the diff is materially smaller than the new content.

Both engage only above CCH_DELTA_MIN_BYTES (default 1000 — around the p90 of
observed command output, so the common tiny result is untouched) and only for
successful commands.
"""
import difflib
import hashlib
import os
import time
from pathlib import Path
from typing import Optional

from lib.ccm_cache import retrieve_content

LEDGER_DIR = Path.home() / '.claude' / 'cache' / 'cch' / 'delta'
MIN_BYTES = int(os.environ.get('CCH_DELTA_MIN_BYTES', '1000'))
LEDGER_TTL_S = 24 * 3600
DIFF_MAX_RATIO = 0.6      # a diff must beat this fraction of the full content


def _sig(session: str, command: str) -> Path:
    digest = hashlib.blake2s(
        f'{session}\x00{command}'.encode('utf-8', 'replace'),
        digest_size=10).hexdigest()
    return LEDGER_DIR / digest


def _prune() -> None:
    try:
        cutoff = time.time() - LEDGER_TTL_S
        for f in LEDGER_DIR.iterdir():
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def previous_key(session: str, command: str) -> Optional[str]:
    try:
        return _sig(session, command).read_text().strip() or None
    except OSError:
        return None


def remember(session: str, command: str, key: str) -> None:
    try:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        _sig(session, command).write_text(key)
        _prune()
    except OSError:
        pass


def emission_for(session: str, command: str, content: str,
                 key: str, exit_code: int = 0) -> Optional[str]:
    """Replacement text for this output, or None to emit it normally.

    Always records the current key so the next run can compare.
    """
    if not session or exit_code != 0 or len(content) < MIN_BYTES:
        if session and exit_code == 0:
            remember(session, command, key)
        return None

    prev = previous_key(session, command)
    remember(session, command, key)
    if not prev:
        return None

    if prev == key:
        lines = content.count('\n')
        return (
            f'[CCM_UNCHANGED {key} · {lines} lines · byte-identical to this '
            f'command earlier in the session]\n'
            f'Retrieve: ccm-get.py {key} [--symbol NAME] [--grep PATTERN]\n'
        )

    old = retrieve_content(prev)
    if not old:
        return None

    diff = ''.join(difflib.unified_diff(
        old.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile=f'previous ({prev})',
        tofile=f'current ({key})',
        n=2,
    ))
    if not diff or len(diff) >= DIFF_MAX_RATIO * len(content):
        return None
    if not diff.endswith('\n'):
        diff += '\n'
    return (
        f'[CCM_DELTA {key} · diff vs previous output of this command '
        f'({len(diff)}B of {len(content)}B)]\n'
        f'{diff}'
        f'Retrieve full: ccm-get.py {key} [--symbol NAME] [--grep PATTERN]\n'
    )
