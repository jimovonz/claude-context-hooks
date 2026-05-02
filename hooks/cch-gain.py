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
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

EVENTS_LOG = Path.home() / '.claude' / 'cache' / 'ccm' / 'events.jsonl'
# retrieval.log lives next to the canonical script, found via the symlink target.
RETRIEVAL_LOG = Path(__file__).resolve().parent / 'retrieval.log'

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


def main() -> int:
    p = argparse.ArgumentParser(prog='cch-gain', description='Token-savings report for claude-context-hooks')
    p.add_argument('--days', type=int, default=30, help='Window in days (default 30)')
    p.add_argument('--since', help='ISO date, overrides --days')
    p.add_argument('--json', action='store_true', help='Emit JSON instead of text report')
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

    agg = aggregate(since)

    if args.json:
        out = {'since': since.isoformat(), 'days': days, **agg}
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render_text(agg, since, days))
    return 0


if __name__ == '__main__':
    sys.exit(main())
