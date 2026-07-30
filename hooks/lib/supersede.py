"""Supersession index — the one case where eviction is provably safe.

`delta.py` keeps a forward ledger, (session, command) -> latest key, which
answers "has this command's output changed?". The proxy needs the opposite
question — "is THIS key stale, and is its replacement already in front of the
model?" — and a forward ledger cannot answer it.

Eviction is unsafe in general because the model cannot perceive what it is
missing: at eviction time the relevance has not manifested, and once it does
the evidence is gone. Superseded content is the exception, and the only one.
Its replacement is already in context, so nothing must be recognised and
nothing can fail to be.

On-disk format is the contract (docs/CONTRACT.md §3); this module is a
reference implementation the proxy may use or reimplement.

    ~/.claude/cache/cch/superseded/<hex-of-old-key>  ->  newer key

Entries are monotone — never rewritten to a different value — because the
proxy's transform must be deterministic across requests. A transform that
changes its mind rebuilds the cached prefix every time it does.
"""
import os
import time
from pathlib import Path
from typing import Iterable, List, Optional

SUPERSEDED_DIR = Path.home() / '.claude' / 'cache' / 'cch' / 'superseded'
MAX_CHAIN = 8
_PRUNE_TTL_DAYS = int(os.environ.get('CCH_CACHE_TTL_DAYS', '14'))


def _hex(key: str) -> str:
    return key.split(':', 1)[1] if ':' in key else key


def record(old_key: str, new_key: str) -> bool:
    """Note that old_key was replaced by new_key. Returns True if written.

    Monotone: an existing entry is never changed. A later run producing a
    third key extends the chain (K1->K2, K2->K3) rather than rewriting K1.
    """
    if not old_key or not new_key or old_key == new_key:
        return False
    try:
        SUPERSEDED_DIR.mkdir(parents=True, exist_ok=True)
        marker = SUPERSEDED_DIR / _hex(old_key)
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, new_key.encode('utf-8'))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def superseded_by(key: str) -> Optional[str]:
    """The key that directly replaced `key`, or None."""
    try:
        value = (SUPERSEDED_DIR / _hex(key)).read_text().strip()
    except OSError:
        return None
    return value or None


def chain(key: str, cap: int = MAX_CHAIN) -> List[str]:
    """Every later key reachable from `key`, nearest first. Cycle-safe."""
    out: List[str] = []
    seen = {key}
    cursor = key
    while len(out) < cap:
        nxt = superseded_by(cursor)
        if not nxt or nxt in seen:
            break
        out.append(nxt)
        seen.add(nxt)
        cursor = nxt
    return out


def _cached(key: str) -> bool:
    try:
        from lib.ccm_cache import _find_blob_path
        return _find_blob_path(key) is not None
    except Exception:
        return False


def prune(ttl_days: Optional[int] = None) -> dict:
    """Drop index entries that can no longer authorise an elision.

    `may_elide` requires `_cached(old_key)`, so once the old key's blob has
    been evicted its marker is dead weight: no future call can return True
    through it. That makes blob-absence — not age — the correct eviction
    signal, and it needs no guess about how long a key stays in context.

    Two guards:
      * an entry whose old key is still cached is kept regardless of age;
      * an entry that another entry POINTS AT is kept even when its own blob
        is gone, because it is an interior link (K1->K2->K3) and dropping it
        would truncate a live predecessor's chain.

    ttl_days is a backstop for markers whose blob was never cached: past the
    cutoff an unreferenced marker goes even if the blob lookup is unavailable.
    """
    ttl_days = _PRUNE_TTL_DAYS if ttl_days is None else ttl_days
    cutoff = time.time() - ttl_days * 86400
    try:
        markers = [p for p in SUPERSEDED_DIR.iterdir() if p.is_file()]
    except OSError:
        return {'removed': 0, 'remaining': 0, 'ttl_days': ttl_days}

    # Keys pointed at by some other entry: interior links of a live chain.
    referenced = set()
    for marker in markers:
        try:
            referenced.add(_hex(marker.read_text().strip()))
        except OSError:
            continue

    # _cached() cannot distinguish "blob absent" from "cannot ask", and both
    # return False, so decide which signal to trust ONCE up front rather than
    # per marker: with the lookup available, absence is authoritative and age
    # is irrelevant; without it, age is all we have.
    lookup_ok = _blob_lookup_available()

    removed = 0
    for marker in markers:
        if marker.name in referenced:
            continue
        if lookup_ok:
            if _cached(f'b2s:{marker.name}'):
                continue
        else:
            try:
                if marker.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
        try:
            marker.unlink()
            removed += 1
        except OSError:
            continue

    return {'removed': removed,
            'remaining': max(0, len(markers) - removed),
            'ttl_days': ttl_days}


def _blob_lookup_available() -> bool:
    """Can we actually ask the cache whether a blob exists?"""
    try:
        from lib.ccm_cache import _find_blob_path  # noqa: F401
        return True
    except Exception:
        return False


def may_elide(key: str, present_keys: Iterable[str]) -> bool:
    """Is it safe to replace this content with its stub?

    All three must hold (docs/CONTRACT.md §4):
      1. the content is still retrievable, so nothing becomes unreachable;
      2. the key is superseded, so it is stale rather than merely old;
      3. its replacement is ALREADY in the message array, so the model is
         not being asked to notice an absence.

    Clause 3 is the safety argument. Without it this is ordinary eviction,
    which is lossy in a way the model cannot detect or report.
    """
    if not key:
        return False
    present = {k for k in present_keys if k}
    if not present:
        return False
    later = chain(key)
    if not later:
        return False
    if not any(k in present for k in later):
        return False
    return _cached(key)
