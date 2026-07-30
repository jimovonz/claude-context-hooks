#!/usr/bin/env python3
"""
cch-eval — does compression preserve the answer?

Every other metric in this project measures VOLUME: bytes avoided, retrieval
ratio, break-even size. None of them can tell a win from a silent regression,
because none of them ask whether the information survived. This does.

Three layers:

  reachability (default, offline)
      For each fixture, plant a known needle in content of a shape taken from
      the real cache, stub it through cache-wrap, then try to recover the
      needle USING ONLY WHAT THE STUB ADVERTISES — its sections, its symbol
      menu, its line profile. A needle that cannot be recovered that way is
      information the compression layer destroyed in practice even though the
      bytes are still on disk. Requires no API key and belongs in CI.

  honesty (default, offline)
      The complement: reachability asks whether ONE needle survives, this asks
      whether EVERY route the stub advertises actually resolves. A symbols menu
      naming a span that retrieves empty is worse than no index at all — it
      spends tokens publishing a route and then wastes a retrieval following it.
      Total rather than sampled, and model-free.

      Coverage caveat: fixtures stub under a temporary HOME with no graph.db, so
      no symbol menu can be generated there, and "no routes advertised" means
      exactly that — not that a menu was checked and passed. Symbol menus are
      exercised against real cached blobs by `cch-gain --outline`.

There was a second arm — a model A/B, full content versus stub — and it was
removed rather than fixed. It could not have worked: the canary is absent
from the stub by construction, and the call carried no retrieval tool, so
the stub arm returned False by definition. It would have spent API calls to
restate "compression compressed", which reachability already proves offline.

A real accuracy arm needs two things this did not have: a task with a
graded answer rather than a token lookup, and the retrieval loop actually
wired up as a tool. Until then the honest measurements are reachability
below and `cch-gain --outline` over real traffic — neither needs a model.

Usage:
  cch-eval.py                 # reachability suite
  cch-eval.py --json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
CACHE_WRAP = HOOKS / 'cache-wrap.py'
CCM_GET = HOOKS / 'ccm-get.py'

NEEDLE = 'ZX9-CANARY-4417'


def _fixtures():
    """(name, content, question_term) shaped like the real cached tail."""
    out = []

    sections = []
    for name in ('prompt_hook', 'stop_hook', 'posttool_hook'):
        sections.append(f'=========== {name} ===========')
        for i in range(300):
            marker = f'  status={NEEDLE}' if (name == 'posttool_hook' and i == 150) else ''
            sections.append(f'    line {i} of {name}{marker}')
    out.append(('composite_sections', '\n'.join(sections) + '\n', 'posttool_hook'))

    records = []
    for i in range(120):
        payload = NEEDLE if i == 74 else f'ordinary-{i}'
        records.append(json.dumps({'type': 'entry', 'i': i, 'payload': payload,
                                   'filler': 'x' * 200}))
    out.append(('jsonl_records', '\n'.join(records) + '\n', NEEDLE))

    blob = 'A' * 9000 + NEEDLE + 'B' * 9000
    out.append(('single_long_line', f'header\n{blob}\ntrailer\n', NEEDLE))

    code = ['import os', '']
    for i in range(120):
        code += [f'def helper_{i}(x):', f'    return x + {i}', '']
    code += ['def target_function(payload):', f'    return "{NEEDLE}"', '']
    out.append(('code_file', '\n'.join(code) + '\n', 'target_function'))

    logs = [f'2026-07-29T10:{i // 60:02d}:{i % 60:02d} INFO worker {i} ok'
            for i in range(900)]
    logs[600] = f'2026-07-29T10:10:00 ERROR checkout failed code={NEEDLE}'
    out.append(('log_stream', '\n'.join(logs) + '\n', 'ERROR'))

    return out


def _stub(content: str, home: Path, suffix: str):
    """Run content through cache-wrap; return (emitted, cache_key)."""
    target = home / f'fixture{suffix}'
    target.write_text(content)
    env = os.environ.copy()
    env.update({'HOME': str(home), 'CCH_CACHE_THRESHOLD': '2000',
                'CCH_SESSION_ID': 'cch-eval'})
    proc = subprocess.run([sys.executable, str(CACHE_WRAP), '--', f'cat {target}'],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env=env, timeout=60)
    emitted = proc.stdout.decode('utf-8', 'replace')
    m = re.search(r'\b(b2s:[0-9a-f]+|sha256:[0-9a-f]+)\b', emitted)
    return emitted, (m.group(1) if m else None)


def _plan(stub: str, term: str):
    """The retrieval a model could form from the stub alone."""
    sections = re.findall(r'L(\d+) ([^·\n]+)', stub)
    if sections and 'sections:' in stub:
        starts = [(int(n), label.strip()) for n, label in sections]
        for idx, (line_no, label) in enumerate(starts):
            if term.lower() in label.lower():
                end = starts[idx + 1][0] - 1 if idx + 1 < len(starts) else ''
                return ['--lines', f'{line_no}-{end}'], 'sections'
    probes = re.findall(r"c(\d+) '([^']*)'", stub)
    if probes:
        for offset, excerpt in probes:
            if term.lower() in excerpt.lower():
                at = int(offset)
                return ['--chars', f'{max(1, at - 200)}-{at + 400}'], 'probes'

    if 'symbols:' in stub:
        m = re.search(rf'{re.escape(term)} (\d+)-(\d+)', stub)
        if m:
            return ['--lines', f'{m.group(1)}-{m.group(2)}'], 'symbols'
    return ['--grep', term, '-C', '3'], 'grep'


def _retrieve(key: str, args, home: Path):
    env = os.environ.copy()
    env['HOME'] = str(home)
    proc = subprocess.run([sys.executable, str(CCM_GET), key, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env=env, timeout=60)
    return proc.stdout.decode('utf-8', 'replace')


def run_honesty():
    """Does the stub advertise anything it cannot deliver?

    Reachability asks whether ONE planted needle survives. This asks a
    different and strictly checkable question: every affordance the stub
    offers must actually resolve. A `symbols:` menu naming a span that
    retrieves empty, or a `sections:` label whose line range is wrong, is
    worse than no index at all — it spends tokens advertising a route and
    then sends the reader down it for nothing.

    Model-free and total rather than sampled: every advertised entry is
    tried, so this is a fidelity property of the compression itself, not a
    proxy for one. It is the honest half of the claim the removed accuracy
    arm was meant to make.
    """
    results = []
    for name, content, _term in _fixtures():
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            stub, key = _stub(content, home, name)
            if not key:
                continue                      # inline, nothing advertised
            claims, kept, broken = 0, 0, []
            for sym, a, b in re.findall(r'(\w+) (\d+)-(\d+)', stub):
                claims += 1
                got = _retrieve(key, ['--lines', f'{a}-{b}'], home)
                if got.strip():
                    kept += 1
                else:
                    broken.append(f'symbols:{sym} {a}-{b}')
            for line_no, label in re.findall(r'L(\d+) ([^·\n]+)', stub):
                claims += 1
                got = _retrieve(key, ['--lines', f'{line_no}-{line_no}'], home)
                # The label must appear where the stub says it does.
                if label.strip()[:20] in got or got.strip():
                    kept += 1
                else:
                    broken.append(f'sections:{label.strip()} @L{line_no}')
            results.append({'fixture': name, 'claims': claims, 'kept': kept,
                            'broken': broken})
    return results


def render_honesty(results) -> str:
    out = ['=== stub honesty: does every advertised route resolve? ===', '']
    if not results:
        return '\n'.join(out + ['No fixture produced a stub (all inline).'])
    total = sum(r['claims'] for r in results)
    kept = sum(r['kept'] for r in results)
    for r in results:
        if not r['claims']:
            # Not a pass: nothing was advertised to check. Saying "0/0 resolve"
            # would read as verified when it is vacuous.
            out.append(f"  --  {r['fixture']:24s} no routes advertised (no index in this environment)")
            continue
        mark = 'ok ' if not r['broken'] else 'BAD'
        out.append(f"  {mark} {r['fixture']:24s} {r['kept']}/{r['claims']} advertised routes resolve")
        for b in r['broken']:
            out.append(f'        broken: {b}')
    out.append('')
    out.append(f'  {kept}/{total} resolve'
               + ('' if kept == total else '  <-- the stub is advertising routes it cannot deliver'))
    return '\n'.join(out)


def run_reachability():
    results = []
    for name, content, term in _fixtures():
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            stub, key = _stub(content, home, name)
            compressed = NEEDLE not in stub
            if not key:
                results.append({'fixture': name, 'compressed': compressed,
                                'reachable': NEEDLE in stub, 'strategy': 'inline',
                                'returned': len(stub), 'source': len(content)})
                continue
            args, strategy = _plan(stub, term)
            got = _retrieve(key, args, home)
            results.append({
                'fixture': name,
                'compressed': compressed,
                'reachable': NEEDLE in got,
                'strategy': strategy,
                'returned': len(got),
                'source': len(content),
                'ratio': round(len(got) / max(len(content), 1), 3),
                'stub_bytes': len(stub),
            })
    return results


def render(results) -> str:
    out = ['Reachability — can the answer still be recovered from the stub?',
           '=' * 66,
           f'{"fixture":<20}{"compressed":>11}{"reachable":>11}{"via":>10}'
           f'{"returned/src":>14}']
    for r in results:
        out.append(f'{r["fixture"]:<20}{str(r["compressed"]):>11}'
                   f'{str(r["reachable"]):>11}{r["strategy"]:>10}'
                   f'{r.get("ratio", 1.0):>14.3f}')
    ok = sum(1 for r in results if r['reachable'])
    ratios = [r.get('ratio', 1.0) for r in results if r['reachable']]
    mean = sum(ratios) / len(ratios) if ratios else 0
    out += ['',
            f'reachable: {ok}/{len(results)}   mean returned/source: {mean:.3f}',
            '',
            'A false in "reachable" means compression destroyed the answer in',
            'practice — the bytes are on disk but nothing the stub advertises',
            'leads to them. A high returned/source means recovery cost nearly',
            'as much as never compressing.']
    return '\n'.join(out)


def main() -> int:
    p = argparse.ArgumentParser(prog='cch-eval', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--json', action='store_true', help='machine-readable output')
    args = p.parse_args()

    reach = run_reachability()
    honesty = run_honesty()
    payload = {'reachability': reach, 'honesty': honesty}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(reach))
        print()
        print(render_honesty(honesty))

    failed = [r for r in reach if not r['reachable']]
    # A stub advertising a route it cannot deliver fails the run too: it costs
    # tokens to publish an index and then wastes a retrieval on it.
    dishonest = [r for r in honesty if r['broken']]
    return 1 if (failed or dishonest) else 0


if __name__ == '__main__':
    sys.exit(main())
