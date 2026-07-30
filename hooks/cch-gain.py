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
import re
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
    """Aggregate the event streams.

    Accounting rule: every row is either what CCH AVOIDED emitting or what it
    DID emit. The old report added cache-wrap savings (original - stub) to
    retrieval savings (source - returned) and counted the same bytes twice —
    row 1 claimed the full output was avoided, row 2 re-claimed it minus the
    slice, and the slice actually returned was never subtracted. A total built
    that way cannot go negative, so it could never answer the question it
    exists for (is the threshold right?).
    """
    cache_wrap = {'cmds': 0, 'cached': 0, 'passthrough': 0,
                  'original_bytes': 0, 'emitted_bytes': 0}
    delta = {'events': 0, 'unchanged': 0, 'diff': 0,
             'original_bytes': 0, 'emitted_bytes': 0}
    retrieval = {'gets': 0, 'source_bytes': 0, 'returned_bytes': 0}
    deny_read = {'count': 0, 'st_size_total': 0}
    deny_edit_write = {'count': 0, 'st_size_total': 0}
    deny_grep = {'count': 0}
    deny_glob = {'count': 0}
    deny_webfetch = {'count': 0}
    graph = {'queries': 0, 'actual_bytes': 0, 'counterfactual_bytes': 0}
    batches = {'runs': 0, 'commands': 0, 'serialized': 0}
    friction = defaultdict(int)
    friction_unattributed = 0
    sessions = set()
    sizes = []

    for row in _read_jsonl(EVENTS_LOG):
        if _parse_ts(row.get('ts', '')) < since:
            continue
        ev = row.get('event')
        sid = row.get('sid') or ''
        if sid:
            sessions.add(sid)
        if ev and (ev.startswith('deny_') or ev.startswith('warn_')):
            # Only attributed rows can produce a per-session rate. Mixing in
            # rows logged before session attribution existed would divide a
            # 90-day deny count by 3 sessions and report nonsense.
            if sid:
                friction[ev] += 1
            else:
                friction_unattributed += 1

        if ev == 'cache_wrap':
            cache_wrap['cmds'] += 1
            original = row.get('original_bytes', 0) or 0
            cache_wrap['original_bytes'] += original
            sizes.append(original)
            if row.get('cached'):
                cache_wrap['cached'] += 1
                cache_wrap['emitted_bytes'] += row.get('stub_bytes', 0) or 0
            else:
                if row.get('passthrough'):
                    cache_wrap['passthrough'] += 1
                cache_wrap['emitted_bytes'] += original
            cmd = row.get('cmd_head', '')
            if 'cairn-graph --' in cmd:
                graph['queries'] += 1
                graph['actual_bytes'] += original
                if '--location' in cmd:
                    graph['counterfactual_bytes'] += max(original * 50, 4000)
                elif any(f in cmd for f in ('--callers', '--callees', '--tests')):
                    graph['counterfactual_bytes'] += max(original * 5, 2000)
                else:
                    graph['counterfactual_bytes'] += max(original * 10, 3000)
        elif ev == 'delta':
            delta['events'] += 1
            delta['original_bytes'] += row.get('original_bytes', 0) or 0
            delta['emitted_bytes'] += row.get('stub_bytes', 0) or 0
            if row.get('kind') == 'unchanged':
                delta['unchanged'] += 1
            else:
                delta['diff'] += 1
        elif ev == 'batch':
            batches['runs'] += 1
            batches['commands'] += row.get('commands', 0) or 0
            batches['serialized'] += row.get('serialized', 0) or 0
        elif ev == 'deny_read':
            deny_read['count'] += 1
            deny_read['st_size_total'] += row.get('st_size', 0) or 0
        elif ev == 'deny_edit':
            deny_edit_write['count'] += 1
            deny_edit_write['st_size_total'] += row.get('st_size', 0) or 0
        elif ev == 'deny_write':
            existing = row.get('st_size_existing', 0) or 0
            if existing > 0:
                # Only counts when overwriting — read-tax applies.
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
        'delta': delta,
        'retrieval': retrieval,
        'deny_read': deny_read,
        'deny_edit_write': deny_edit_write,
        'deny_grep': deny_grep,
        'deny_glob': deny_glob,
        'deny_webfetch': deny_webfetch,
        'graph': graph,
        'batches': batches,
        'friction': dict(friction),
        'friction_unattributed': friction_unattributed,
        'sessions': len(sessions),
        'instruction_bytes': _instruction_block_bytes(),
        'sizes': sizes,
    }


def _instruction_block_bytes() -> int:
    """Size of the routing snippet CCH injects into every session.

    A real, recurring cost of the whole scheme, paid once per session in
    every project. It was never counted anywhere.
    """
    claude_md = Path.home() / '.claude' / 'CLAUDE.md'
    begin = '<!-- BEGIN claude-context-hooks routing policy -->'
    end = '<!-- END claude-context-hooks routing policy -->'
    try:
        text = claude_md.read_text()
    except OSError:
        return 0
    if begin not in text or end not in text:
        return 0
    return len(text.split(begin, 1)[1].split(end, 1)[0].encode('utf-8'))


def _kb(n: int) -> str:
    if not n:
        return '0 kB'
    if abs(n) >= 1024 * 1024:
        return f'{n / 1024 / 1024:.1f} MB'
    return f'{n / 1024:.1f} kB'


def _tokens(n: int) -> str:
    t = _bytes_to_tokens(n)
    if abs(t) >= 1000:
        return f'~{t / 1000:.1f}k tokens'
    return f'~{t} tokens'


def _percentile(sorted_vals, frac: float) -> int:
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * frac))]


def render_text(agg, since: datetime, days: int) -> str:
    out = []
    sessions = agg['sessions']
    header = (f'CCH report (since {since.date().isoformat()}, {days}d window, '
              f'{sessions} attributed session{"" if sessions == 1 else "s"})')
    out.append(header)
    out.append('=' * len(header))
    out.append(f'Token estimate uses {CHARS_PER_TOKEN} chars/token. '
               f'Avoided and emitted are both counted; nothing is counted twice.')
    out.append('')

    cw = agg['cache_wrap']
    d = agg['delta']
    r = agg['retrieval']

    out.append('AVOIDED (observed)')
    out.append(
        f"  Bash output         {cw['cmds']:>6} cmds ({cw['cached']} cached, "
        f"{cw['passthrough']} passthrough): produced {_kb(cw['original_bytes'])}, "
        f"emitted {_kb(cw['emitted_bytes'])}"
    )
    out.append(
        f"  Delta re-reads      {d['events']:>6} hits "
        f"({d['unchanged']} unchanged, {d['diff']} diffs): produced "
        f"{_kb(d['original_bytes'])}, emitted {_kb(d['emitted_bytes'])}"
    )
    out.append('')
    out.append('PAID (observed)')
    out.append(
        f"  Retrieval           {r['gets']:>6} gets: {_kb(r['returned_bytes'])} "
        f"pulled back out of the cache"
    )
    snippet = agg['instruction_bytes']
    snippet_total = snippet * max(sessions, 1)
    out.append(
        f"  Routing snippet     {max(sessions, 1):>6} sessions x {_kb(snippet)} "
        f"injected = {_kb(snippet_total)}"
    )

    avoided = ((cw['original_bytes'] - cw['emitted_bytes'])
               + (d['original_bytes'] - d['emitted_bytes']))
    paid = r['returned_bytes'] + snippet_total
    net = avoided - paid
    out.append('')
    out.append(f"  Net observed:       {_tokens(net)} ({_kb(net)})")
    if net < 0:
        out.append('  ^ negative: the layer is costing more than it saves in '
                   'this window.')
    out.append('')

    out.append('COUNTERFACTUAL (modelled, NOT included in the net above)')
    dr = agg['deny_read']
    dew = agg['deny_edit_write']
    g = agg['graph']
    out.append(f"  Read denies         {dr['count']:>6}: {_kb(dr['st_size_total'])} "
               f"of file content not read   [st_size]")
    out.append(f"  Edit/Write/NB       {dew['count']:>6}: {_kb(dew['st_size_total'])} "
               f"read-tax avoided           [st_size]")
    out.append(f"  Graph queries       {g['queries']:>6}: {_kb(g['actual_bytes'])} "
               f"actual vs {_kb(g['counterfactual_bytes'])} modelled")
    out.append(f"  Grep/Glob/WebFetch  "
               f"{agg['deny_grep']['count'] + agg['deny_glob']['count'] + agg['deny_webfetch']['count']:>6}"
               f": no saving claimed")
    out.append('')

    # Friction: the project's acceptance signal is ~1-2 corrections/session.
    # It was defined in CLAUDE.md and never measured, because no event
    # carried a session id.
    out.append('FRICTION (acceptance signal: ~1-2 corrections per session)')
    friction = agg['friction']
    if not friction:
        out.append('  none recorded in attributed sessions')
    else:
        denom = max(sessions, 1)
        for ev, count in sorted(friction.items(), key=lambda kv: -kv[1]):
            out.append(f'  {ev:<20}{count:>6}   {count / denom:>5.1f}/session')
        total_f = sum(friction.values())
        verdict = ('on target' if 1 <= total_f / denom <= 2
                   else 'too lax' if total_f / denom < 1 else 'too aggressive')
        out.append(f'  {"TOTAL":<20}{total_f:>6}   {total_f / denom:>5.1f}/session '
                   f'({verdict})')
    if agg['friction_unattributed']:
        out.append(f"  ({agg['friction_unattributed']} further corrections carry "
                   f"no session id — logged before session attribution existed, "
                   f"so they are excluded from the rates above)")
    out.append('')

    b = agg['batches']
    if b['runs']:
        out.append(f"BATCHING  {b['runs']} batches, {b['commands']} commands "
                   f"({b['commands'] / b['runs']:.1f} per batch, "
                   f"{b['serialized']} same-file serialized)")
        out.append('')

    sizes = sorted(agg['sizes'])
    if sizes:
        p50, p90, p99 = (_percentile(sizes, 0.5), _percentile(sizes, 0.9),
                         _percentile(sizes, 0.99))
        out.append(f'OUTPUT SIZE  p50 {p50}B · p90 {p90}B · p99 {p99}B · '
                   f'max {sizes[-1]}B')
        out.append(f'  A threshold near {p90}B would cache ~10% of commands; '
                   f'cached {cw["cached"]} of {cw["cmds"]} '
                   f'({100 * cw["cached"] / max(cw["cmds"], 1):.1f}%) in this window.')
        out.append('')

    out.append('NOTE: counts only per-event effects. Invisible downstream')
    out.append('avoidance (re-loaded files, session restarts from window blowout,')
    out.append('model confusion from truncation) is NOT captured. For session-level')
    out.append('ground truth, A/B with CCH_DISABLE=1 set vs unset.')
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


def render_outline(since: datetime, days: int) -> str:
    """Does an index in the stub change retrieval behaviour?

    The question the section outline exists to answer, made repeatable.
    Groups cached blobs by what their stub carried, then joins to
    retrieval.log. A shipped index that does not move the retrieval rate
    or the returned fraction is costing tokens for nothing — which is
    what the earlier symbol-menu data showed (--symbol reached 0.4% use).
    """
    ret = {}
    for row in _read_jsonl(RETRIEVAL_LOG):
        if _parse_ts(row.get('timestamp', '')) < since:
            continue
        key = (row.get('key') or '').rstrip('.')
        if not key:
            continue
        agg = ret.setdefault(key, {'gets': 0, 'back': 0, 'src': 0, 'filters': defaultdict(int)})
        agg['gets'] += 1
        agg['back'] += row.get('returned_bytes', 0) or 0
        agg['src'] = max(agg['src'], row.get('source_size', 0) or 0)
        for name, val in (row.get('filter') or {}).items():
            if val not in (None, False, 0):
                agg['filters'][name] += 1

    groups = {}
    for row in _read_jsonl(EVENTS_LOG):
        if _parse_ts(row.get('ts', '')) < since:
            continue
        if row.get('event') != 'cache_wrap' or not row.get('cached'):
            continue
        key = (row.get('cache_key') or '').rstrip('.')
        if row.get('outline_sections'):
            name = 'sections index'
        elif row.get('has_menu'):
            name = 'symbol menu'
        elif 'outline_sections' in row:
            name = 'profile only'
        else:
            name = 'no index (legacy)'
        g = groups.setdefault(name, {'n': 0, 'retrieved': 0, 'back': 0, 'src': 0,
                                     'idx_bytes': 0, 'filters': defaultdict(int)})
        g['n'] += 1
        g['idx_bytes'] += row.get('outline_bytes', 0) or 0
        hit = ret.get(key)
        if hit:
            g['retrieved'] += 1
            g['back'] += hit['back']
            g['src'] += max(hit['src'], row.get('original_bytes', 0) or 0)
            for name_f, count in hit['filters'].items():
                g['filters'][name_f] += count

    out = [f'Stub index effectiveness (since {since.date().isoformat()}, {days}d)',
           '=' * 62,
           'Hypothesis: a stub that carries an index is retrieved less often,',
           'and more narrowly, than one that does not.',
           '']
    out.append(f'{"stub carried":<20}{"n":>5}{"retrieved":>11}{"rate":>7}'
               f'{"ret/src":>9}{"index cost":>12}')
    for name, g in sorted(groups.items(), key=lambda kv: -kv[1]['n']):
        rate = 100 * g['retrieved'] / max(g['n'], 1)
        frac = g['back'] / max(g['src'], 1)
        cost = g['idx_bytes'] / max(g['n'], 1)
        out.append(f'{name:<20}{g["n"]:>5}{g["retrieved"]:>11}{rate:>6.0f}%'
                   f'{frac:>9.2f}{cost:>10.0f}B')

    out.append('')
    out.append('filter mix by group (what the model reached for):')
    for name, g in sorted(groups.items(), key=lambda kv: -kv[1]['n']):
        total = sum(g['filters'].values())
        if not total:
            continue
        mix = ' · '.join(f'{f} {100 * c / total:.0f}%'
                         for f, c in sorted(g['filters'].items(), key=lambda kv: -kv[1]))
        out.append(f'  {name:<18} {mix}')

    if not groups:
        out.append('  (no cached events in window)')
    out.append('')
    out.append('Read it this way: if "sections index" does not show a lower rate or')
    out.append('a lower ret/src than "profile only", the index is not being used and')
    out.append('the generators are not worth extending.')
    return '\n'.join(out)


# Navigation intent, classified from cmd_head. The graph answers the first
# group directly; the other two are the model navigating by reading instead.
_NAV_GRAPH = re.compile(r'(?<![-\w])cairn-graph\b')
_NAV_CODE_EXT = r'\.(?:py|js|ts|tsx|jsx|c|h|cc|cpp|hpp|rs|go|java|rb|v|sv|sh)\b'
_NAV_READ = re.compile(r'(?<![-\w])(?:sed|cat|head|tail)\b')
_NAV_SEARCH = re.compile(r'(?<![-\w])(?:rg|grep|egrep|fgrep)\b')
_NAV_IDENT = re.compile(r'''["']?(?:(?:def|class|fn|func|function)\s+)?([A-Za-z_][A-Za-z0-9_]{2,})["']?''')


def _nav_kind(cmd: str):
    """'graph' | 'search' | 'read' | None — how this command navigated code.

    Only the first pipeline segment is the navigation act; a trailing
    `| grep x` narrows a result set rather than locating anything.
    """
    head = cmd.split('|')[0]
    if _NAV_GRAPH.search(head):
        return 'graph'
    if _NAV_SEARCH.search(head) and _NAV_IDENT.search(head):
        return 'search'
    if _NAV_READ.search(head) and re.search(_NAV_CODE_EXT, head):
        return 'read'
    return None


def render_tools(since: datetime, days: int) -> str:
    """Is the graph being used where it is the cheaper tool?

    graph-first% = graph / (graph + symbol-shaped search + code read). The
    denominator is navigation the graph could have served, so this measures
    tool choice, not activity. Reported per session because that is the unit
    an intervention acts on — a lifetime number cannot be a control arm,
    since it moves only as fast as its own tail.

    Session attribution arrived with the 2026-07 review pass, so commands
    logged before it carry no sid and are counted only in the totals line.
    """
    overall = defaultdict(int)
    per_sid = defaultdict(lambda: defaultdict(int))
    for row in _read_jsonl(EVENTS_LOG):
        if _parse_ts(row.get('ts', '')) < since:
            continue
        if row.get('event') != 'cache_wrap':
            continue
        kind = _nav_kind(row.get('cmd_head') or '')
        if not kind:
            continue
        overall[kind] += 1
        sid = row.get('sid')
        if sid:
            per_sid[sid][kind] += 1

    def _rate(c) -> float:
        denom = c['graph'] + c['search'] + c['read']
        return 100.0 * c['graph'] / denom if denom else 0.0

    out = [f'=== Tool utilisation, last {days}d (since {since.date()}) ===', '']
    denom = overall['graph'] + overall['search'] + overall['read']
    if not denom:
        return '\n'.join(out + ['No navigation commands in window.'])
    out.append(f'  navigation commands: {denom}')
    out.append(f'    cairn-graph          {overall["graph"]:6d}')
    out.append(f'    symbol-shaped search {overall["search"]:6d}')
    out.append(f'    code file read       {overall["read"]:6d}')
    out.append(f'  GRAPH-FIRST: {_rate(overall):.1f}%')
    out.append('')

    navsess = {s: c for s, c in per_sid.items()
               if c['graph'] + c['search'] + c['read'] >= 5}
    if not navsess:
        out.append('  No attributed session has >=5 navigation commands yet —')
        out.append('  per-session rates need sid coverage to accumulate before')
        out.append('  they can serve as a baseline or an A/B arm.')
        return '\n'.join(out)

    out.append(f'  by session (>=5 navigation commands, n={len(navsess)}):')
    out.append(f'    {"session":16s} {"graph":>6s} {"search":>7s} {"read":>6s} {"graph-first":>12s}')
    for sid, c in sorted(navsess.items(), key=lambda kv: -_rate(kv[1])):
        out.append(f'    {sid[:16]:16s} {c["graph"]:6d} {c["search"]:7d} '
                   f'{c["read"]:6d} {_rate(c):11.1f}%')
    rates = [_rate(c) for c in navsess.values()]
    out.append('')
    out.append(f'  median session graph-first: {sorted(rates)[len(rates) // 2]:.1f}%')
    out.append(f'  sessions at 0%: {sum(1 for r in rates if r == 0)}/{len(rates)}')
    return '\n'.join(out)


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
    p.add_argument('--outline', action='store_true', help='Did stub indexes change retrieval behaviour? (sections vs profile-only)')
    p.add_argument('--tools', action='store_true', help='graph-first%% — is cairn-graph used where it is the cheaper tool?')
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

    if args.outline:
        print(render_outline(since, days))
        return 0

    if args.tools:
        print(render_tools(since, days))
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
