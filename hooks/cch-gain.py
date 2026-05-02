#!/usr/bin/env python3
"""
cch-gain — token-savings report for claude-context-hooks.

Reads two log streams:
  - ~/.claude/cache/ccm/events.jsonl  (cache-wrap + intercept denies)
  - hooks/retrieval.log               (ccm-get.py retrievals)

Aggregates and reports savings with disclosed methodology per row.

Honest measurements:
  [observed]              both sides directly seen
  [counterfactual]        one side computed deterministically (st_size)

Sections:
  Cache wrapper           [observed]
  Retrieval (ccm-get.py)  [observed]
  Read denies             [counterfactual: st_size]
  Edit/Write/NB denies    [counterfactual: read-tax st_size]
  Grep/Glob denies        no direct saving claimed (downstream cache-wrap)
  WebFetch denies         not measurable (built-in summarizes vs raw)

Token estimate: bytes / 4 (rough English-text heuristic).

Usage:
  cch-gain.py                 # last 30 days
  cch-gain.py --days 7
  cch-gain.py --since 2026-04-01
  cch-gain.py --json          # machine-readable
  cch-gain.py --dist          # cache_wrap original_bytes histogram + threshold trial
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

EVENTS_LOG = Path.home() / '.claude' / 'cache' / 'ccm' / 'events.jsonl'
RETRIEVAL_LOG = Path.home() / '.claude' / 'cache' / 'ccm' / 'retrieval.log'

CHARS_PER_TOKEN = 4


def _bytes_to_tokens(n_bytes: int) -> int:
    return n_bytes // CHARS_PER_TOKEN


def _read_jsonl(path: Path):
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_ts(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.min


def aggregate(since: datetime):
    cache_wrap = {'cmds': 0, 'cached': 0, 'original_bytes': 0, 'stub_bytes': 0}
    retrieval = {'gets': 0, 'source_bytes': 0, 'returned_bytes': 0}
    deny_read = {'count': 0, 'st_size_total': 0}
    deny_edit_write = {'count': 0, 'st_size_total': 0}  # Edit + Write(existing) + NotebookEdit
    deny_grep = {'count': 0}
    deny_glob = {'count': 0}
    deny_webfetch = {'count': 0}

    for row in _read_jsonl(EVENTS_LOG):
        if _parse_ts(row.get('ts', '')) < since:
            continue
        ev = row.get('event')
        if ev == 'cache_wrap':
            cache_wrap['cmds'] += 1
            cache_wrap['original_bytes'] += row.get('original_bytes', 0) or 0
            if row.get('cached'):
                cache_wrap['cached'] += 1
                cache_wrap['stub_bytes'] += row.get('stub_bytes', 0) or 0
            else:
                # Below-threshold: stub == original (no saving)
                cache_wrap['stub_bytes'] += row.get('original_bytes', 0) or 0
        elif ev == 'deny_read':
            deny_read['count'] += 1
            deny_read['st_size_total'] += row.get('st_size', 0) or 0
        elif ev == 'deny_edit':
            deny_edit_write['count'] += 1
            deny_edit_write['st_size_total'] += row.get('st_size', 0) or 0
        elif ev == 'deny_write':
            existing = row.get('st_size_existing', 0) or 0
            if existing > 0:
                # Only counts when overwriting — read-tax applies. New-file Write has no saving.
                deny_edit_write['count'] += 1
                deny_edit_write['st_size_total'] += existing
        elif ev == 'deny_notebookedit':
            deny_edit_write['count'] += 1
            deny_edit_write['st_size_total'] += row.get('st_size', 0) or 0
        elif ev == 'deny_grep':
            deny_grep['count'] += 1
        elif ev == 'deny_glob':
            deny_glob['count'] += 1
        elif ev == 'deny_webfetch':
            deny_webfetch['count'] += 1

    for row in _read_jsonl(RETRIEVAL_LOG):
        if _parse_ts(row.get('timestamp', '')) < since:
            continue
        retrieval['gets'] += 1
        retrieval['source_bytes'] += row.get('source_size', 0) or 0
        retrieval['returned_bytes'] += row.get('returned_bytes', 0) or 0

    return {
        'cache_wrap': cache_wrap,
        'retrieval': retrieval,
        'deny_read': deny_read,
        'deny_edit_write': deny_edit_write,
        'deny_grep': deny_grep,
        'deny_glob': deny_glob,
        'deny_webfetch': deny_webfetch,
    }


def _kb(n: int) -> str:
    return f'{n / 1024:.1f} kB' if n else '0 kB'


def _tokens(n: int) -> str:
    t = _bytes_to_tokens(n)
    if t >= 1000:
        return f'~{t / 1000:.1f}k tokens'
    return f'~{t} tokens'


def render_text(agg, since: datetime, days: int) -> str:
    out = []
    header = f'CCH Token Savings (since {since.date().isoformat()}, {days}d window)'
    out.append(header)
    out.append('=' * len(header))
    out.append(f"Token estimate uses {CHARS_PER_TOKEN} chars/token. Methodology disclosed per row.")
    out.append('')

    cw = agg['cache_wrap']
    cw_saved = cw['original_bytes'] - cw['stub_bytes']
    out.append(
        f"Cache wrapper:     {cw['cmds']:>4} cmds ({cw['cached']} cached), "
        f"{_kb(cw['stub_bytes'])} of {_kb(cw['original_bytes'])} returned "
        f"-> saved {_tokens(cw_saved)}   [observed]"
    )

    r = agg['retrieval']
    r_saved = r['source_bytes'] - r['returned_bytes']
    out.append(
        f"Retrieval:         {r['gets']:>4} gets, "
        f"{_kb(r['returned_bytes'])} returned of {_kb(r['source_bytes'])} cached "
        f"-> saved {_tokens(r_saved)}   [observed]"
    )

    dr = agg['deny_read']
    out.append(
        f"Read denies:       {dr['count']:>4} denies, "
        f"counterfactual {_kb(dr['st_size_total'])} of file content avoided "
        f"-> saved {_tokens(dr['st_size_total'])}   [counterfactual: st_size]"
    )

    dew = agg['deny_edit_write']
    out.append(
        f"Edit/Write/NB:     {dew['count']:>4} denies, "
        f"counterfactual {_kb(dew['st_size_total'])} read-tax avoided "
        f"-> saved {_tokens(dew['st_size_total'])}   [counterfactual: read-tax st_size]"
    )

    out.append(
        f"Grep denies:       {agg['deny_grep']['count']:>4} denies, "
        f"no direct saving claimed (downstream cache-wrap captures wins)"
    )
    out.append(
        f"Glob denies:       {agg['deny_glob']['count']:>4} denies, "
        f"no direct saving claimed (downstream cache-wrap captures wins)"
    )
    out.append(
        f"WebFetch denies:   {agg['deny_webfetch']['count']:>4} denies, "
        f"savings not measurable (built-in summarizes vs curl returns raw)"
    )

    total = cw_saved + r_saved + dr['st_size_total'] + dew['st_size_total']
    out.append('')
    out.append(f"Total honest savings: {_tokens(total)} ({_kb(total)})")
    out.append('')
    out.append("NOTE: Counts only visible per-event savings. Invisible downstream")
    out.append("avoidance (re-loaded files, session restarts from window blowout,")
    out.append("model confusion from truncation) is NOT captured. For session-level")
    out.append("ground truth, A/B with CCH_DISABLE=1 set vs unset.")
    return '\n'.join(out)


BUCKETS = [
    ('<0.5kB ', 0,     500),
    ('0.5-1kB', 500,   1000),
    ('1-2kB  ', 1000,  2000),
    ('2-4kB  ', 2000,  4000),
    ('4-6kB  ', 4000,  6000),
    ('6-8kB  ', 6000,  8000),
    ('8-16kB ', 8000,  16000),
    ('>16kB  ', 16000, float('inf')),
]

CANDIDATE_THRESHOLDS = [500, 1000, 2000, 4000, 8000, 16000]


def render_dist(since: datetime, days: int) -> str:
    sizes = []
    for row in _read_jsonl(EVENTS_LOG):
        if _parse_ts(row.get('ts', '')) < since:
            continue
        if row.get('event') != 'cache_wrap':
            continue
        n = row.get('original_bytes', 0) or 0
        sizes.append(n)

    out = []
    header = f'Cache wrapper distribution (since {since.date().isoformat()}, {days}d window)'
    out.append(header)
    out.append('=' * len(header))

    if not sizes:
        out.append('No cache_wrap events in window — run more sessions and retry.')
        return '\n'.join(out)

    n = len(sizes)
    out.append(f'n = {n} events, sum = {sum(sizes) / 1024:.1f} kB, '
               f'avg = {sum(sizes) // n} B, max = {max(sizes)} B')
    out.append('')
    out.append(f'  {"Bucket":<10} {"Count":>5}  {"%":>6}  bar')
    for label, lo, hi in BUCKETS:
        c = sum(1 for s in sizes if lo <= s < hi)
        pct = 100 * c / n
        bar = '#' * c if c < 60 else '#' * 60 + f' (+{c-60})'
        out.append(f'  {label:<10} {c:>5}  {pct:>5.1f}%  {bar}')

    out.append('')
    out.append('Threshold trial — outputs that WOULD be cached at each candidate:')
    out.append(f'  {"threshold":<14} {"would-cache":>11}  {"%":>6}')
    for t in CANDIDATE_THRESHOLDS:
        c = sum(1 for s in sizes if s > t)
        pct = 100 * c / n
        marker = '   <-- current default' if t == 8000 else ''
        out.append(f'  >{t:>5} bytes    {c:>11}  {pct:>5.1f}%{marker}')

    out.append('')
    out.append('Pick a threshold near the floor of where slice-retrieval beats inline.')
    out.append('Stub overhead ~150 bytes, so caching outputs <500B is always net-negative.')
    out.append('Set via env: export CCH_CACHE_THRESHOLD=<bytes>')
    return '\n'.join(out)


def _cmd_signature(cmd_head: str) -> tuple:
    """Reduce a command to (cmd, primary_target) for retry detection.

    Strips flags so 'find /usr -type f' and 'find /usr -type d' both
    signature as ('find', '/usr'). Crude but it catches the common
    'model didn't get what it wanted, ran the same command with a
    different flag' pattern.
    """
    toks = [t for t in cmd_head.split()[:6] if not t.startswith('-')]
    return tuple(toks[:2])


def render_retrieval(since: datetime, days: int) -> str:
    """Per-cache retrieval-ratio analysis.

    For each cache_wrap event with cached=True in the window, find all
    matching ccm-get retrievals (joined on cache_key) and compute the
    fraction of the original bytes that the model actually pulled back.

    A cache that was never retrieved is 'orphaned' — the model emitted
    the stub and moved on, paying ~138 tokens of round-trip overhead
    for content it never used. Orphan rate is the headline metric for
    threshold tuning.

    Also detects RETRY pattern: after a retrieval, did the model
    immediately re-issue a similar Bash command? That signals the
    slice didn't give what was needed and the cache failed at its job.
    """
    # Build chronologically-ordered list of ALL cache_wrap events
    all_cw = []
    for row in _read_jsonl(EVENTS_LOG):
        if row.get('event') != 'cache_wrap':
            continue
        ts = _parse_ts(row.get('ts', ''))
        all_cw.append((ts, row))
    all_cw.sort(key=lambda x: x[0])

    caches = {}  # full_key -> {original, cmd_head, ts}
    for ts, row in all_cw:
        if ts < since:
            continue
        if not row.get('cached'):
            continue
        key = row.get('cache_key', '')
        if key:
            caches[key] = {
                'original_bytes': row.get('original_bytes', 0) or 0,
                'cmd_head': row.get('cmd_head', ''),
                'ts': row.get('ts', ''),
                'ts_parsed': ts,
            }

    # Index retrievals by their (truncated) key prefix
    retrievals = {}  # key_prefix -> list of returned_bytes
    for row in _read_jsonl(RETRIEVAL_LOG):
        if _parse_ts(row.get('timestamp', '')) < since:
            continue
        # retrieval.log stores key as full_key[:20] + '...'
        rkey = (row.get('key') or '').rstrip('.')
        if not rkey:
            continue
        retrievals.setdefault(rkey, []).append({
            'returned_bytes': row.get('returned_bytes', 0) or 0,
            'source_size': row.get('source_size', 0) or 0,
            'is_full': row.get('is_full_retrieval', False),
        })

    out = []
    header = f'Cache retrieval-ratio analysis (since {since.date().isoformat()}, {days}d window)'
    out.append(header)
    out.append('=' * len(header))

    if not caches:
        out.append('No cached cache_wrap events in window — threshold may be set too high or workload is RTK-shrunk below it.')
        return '\n'.join(out)

    # Build retrieval -> timestamp index for retry detection
    retrieval_ts = {}  # key_prefix -> [(ts_parsed, ...), ...]
    for row in _read_jsonl(RETRIEVAL_LOG):
        ts = _parse_ts(row.get('timestamp', ''))
        if ts < since:
            continue
        rkey = (row.get('key') or '').rstrip('.')
        if rkey:
            retrieval_ts.setdefault(rkey, []).append(ts)

    rows = []
    orphans = 0
    full_pulls = 0
    retry_count = 0
    RETRY_WINDOW_SEC = 300  # 5 minutes
    for full_key, c in caches.items():
        prefix = full_key[:20]
        rs = retrievals.get(prefix, [])
        total_returned = sum(r['returned_bytes'] for r in rs)
        ratio = total_returned / c['original_bytes'] if c['original_bytes'] else 0
        if not rs:
            orphans += 1
        if any(r['is_full'] for r in rs):
            full_pulls += 1

        # Retry detection: was the next cache_wrap event after the LAST
        # retrieval a similar command (same signature) within the window?
        retry = False
        ts_list = retrieval_ts.get(prefix, [])
        if ts_list:
            last_retrieval = max(ts_list)
            sig = _cmd_signature(c['cmd_head'])
            for next_ts, next_row in all_cw:
                if next_ts <= last_retrieval:
                    continue
                if (next_ts - last_retrieval).total_seconds() > RETRY_WINDOW_SEC:
                    break
                if _cmd_signature(next_row.get('cmd_head', '')) == sig:
                    retry = True
                    break
        if retry:
            retry_count += 1

        rows.append((full_key, c, len(rs), total_returned, ratio, retry))

    n = len(caches)
    out.append(f'n = {n} cached events, {orphans} orphaned ({100*orphans/n:.0f}% never retrieved), '
               f'{full_pulls} full-pulls ({100*full_pulls/n:.0f}%), '
               f'{retry_count} retries ({100*retry_count/n:.0f}% — slice didn\'t satisfy)')
    out.append('')
    out.append('Retrieval-ratio buckets (per-cache total_returned / original_bytes):')
    rb = [('  0% (orphan)', lambda r: r == 0),
          ('  <10%',         lambda r: 0 < r < 0.10),
          ('  10-50%',       lambda r: 0.10 <= r < 0.50),
          ('  50-90%',       lambda r: 0.50 <= r < 0.90),
          ('  >=90%',        lambda r: r >= 0.90)]
    for label, pred in rb:
        c = sum(1 for _, _, _, _, ratio, _ in rows if pred(ratio))
        bar = '#' * c if c < 60 else '#' * 60 + f' (+{c-60})'
        out.append(f'  {label:<14} {c:>4}  ({100*c/n:>5.1f}%)  {bar}')

    out.append('')
    out.append('Implications:')
    out.append('  * orphan rate high  -> threshold too low; caching content the model never reads')
    out.append('  * full-pull rate high -> threshold too low OR slicing tools too coarse for workload')
    out.append('  * retry rate high   -> slice did not give what model wanted (re-issued similar bash within 5min)')
    out.append('  * 10-50% slice band -> caching is doing real work; tighten threshold to grow this band')
    return '\n'.join(out)


def main() -> int:
    p = argparse.ArgumentParser(prog='cch-gain', description='Token-savings report for claude-context-hooks')
    p.add_argument('--days', type=int, default=30, help='Window in days (default 30)')
    p.add_argument('--since', help='ISO date, overrides --days')
    p.add_argument('--json', action='store_true', help='Emit JSON instead of text report')
    p.add_argument('--dist', action='store_true', help='Print cache_wrap original_bytes histogram + threshold trial')
    p.add_argument('--retrieval', action='store_true', help='Per-cache retrieval-ratio analysis (orphan rate + slice distribution)')
    args = p.parse_args()

    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
            days = (datetime.now() - since).days
        except ValueError:
            print(f'cch-gain: invalid --since date: {args.since}', file=sys.stderr)
            return 1
    else:
        since = datetime.now() - timedelta(days=args.days)
        days = args.days

    if args.dist and args.json:
        # JSON form of the histogram
        sizes = []
        for row in _read_jsonl(EVENTS_LOG):
            if _parse_ts(row.get('ts', '')) < since:
                continue
            if row.get('event') != 'cache_wrap':
                continue
            sizes.append(row.get('original_bytes', 0) or 0)
        buckets = [{'label': label.strip(), 'lo': lo, 'hi': hi if hi != float('inf') else None,
                    'count': sum(1 for s in sizes if lo <= s < hi)}
                   for label, lo, hi in BUCKETS]
        trials = [{'threshold': t, 'would_cache': sum(1 for s in sizes if s > t)}
                  for t in CANDIDATE_THRESHOLDS]
        print(json.dumps({'since': since.isoformat(), 'days': days,
                          'n': len(sizes), 'buckets': buckets, 'trials': trials}, indent=2))
        return 0

    if args.dist:
        print(render_dist(since, days))
        return 0

    if args.retrieval:
        print(render_retrieval(since, days))
        return 0

    agg = aggregate(since)

    if args.json:
        out = {'since': since.isoformat(), 'days': days, **agg}
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render_text(agg, since, days))
    return 0


if __name__ == '__main__':
    sys.exit(main())
