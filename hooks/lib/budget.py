"""Shared, finite full-content budget over a rolling window.

Full access to content is a finite resource, not an honor-system escape
hatch. cache-wrap's uncached passthrough already worked this way; ccm-get's
`--grep "."` full retrieval did not — it gated on a 20-character --reason
string, which is satisfied trivially and was (observed in this repo's own
review session) satisfied twice without friction. Both now draw on ONE pool,
so spending it in either place is felt in the other.

Concurrency: the previous JSON read-modify-write in cache-wrap had no
locking, and cch-batch runs up to --jobs commands at once — concurrent
grants each read the same balance and the last writer won. All access here
is serialized with flock.
"""
import fcntl
import json
import os
import time
from pathlib import Path

from lib.event_log import log_event

BUDGET_TOKENS = int(os.environ.get('CCH_PASSTHROUGH_BUDGET', '25000'))
WINDOW_S = 5 * 3600
STATE_FILE = Path.home() / '.claude' / 'cache' / 'passthrough_budget.json'


def _fresh(now: float) -> dict:
    return {'window_start': now, 'spent': 0, 'grants': 0}


def _read_locked(fh) -> dict:
    fh.seek(0)
    raw = fh.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    now = time.time()
    try:
        start = float(data.get('window_start', 0) or 0)
    except (TypeError, ValueError):
        start = 0.0
    if now - start >= WINDOW_S:
        return _fresh(now)
    data.setdefault('spent', 0)
    data.setdefault('grants', 0)
    data['window_start'] = start
    return data


def _open_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    return open(STATE_FILE, 'a+', encoding='utf-8')


def status() -> tuple[int, int, float]:
    """(spent, total, hours_until_reset) — reads without spending."""
    if BUDGET_TOKENS <= 0:
        return 0, 0, 0.0
    try:
        with _open_state() as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            data = _read_locked(fh)
    except OSError:
        return 0, BUDGET_TOKENS, WINDOW_S / 3600
    resets_h = (WINDOW_S - (time.time() - data['window_start'])) / 3600
    return int(data['spent']), BUDGET_TOKENS, max(0.0, resets_h)


def spend(tokens: int, kind: str = '') -> tuple[bool, int, float]:
    """Try to spend `tokens` from the window.

    Returns (granted, remaining_after, hours_until_reset). A failure to
    account (unwritable state) denies the grant rather than granting for
    free — the whole point is that the resource is real.
    """
    if BUDGET_TOKENS <= 0 or tokens <= 0:
        return False, 0, 0.0
    try:
        with _open_state() as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            data = _read_locked(fh)
            remaining = BUDGET_TOKENS - int(data['spent'])
            if tokens > remaining:
                resets_h = (WINDOW_S - (time.time() - data['window_start'])) / 3600
                log_event('budget_denied', kind=kind, wanted=tokens,
                          remaining=max(0, remaining))
                return False, max(0, remaining), max(0.0, resets_h)
            data['spent'] = int(data['spent']) + tokens
            data['grants'] = int(data.get('grants', 0)) + 1
            if kind:
                counts = data.setdefault('by_kind', {})
                counts[kind] = int(counts.get(kind, 0)) + tokens
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(data))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        return False, 0, 0.0
    log_event('budget_spend', kind=kind, tokens=tokens,
              remaining=BUDGET_TOKENS - data['spent'])
    resets_h = (WINDOW_S - (time.time() - data['window_start'])) / 3600
    return True, BUDGET_TOKENS - data['spent'], max(0.0, resets_h)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
