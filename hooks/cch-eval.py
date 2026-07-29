#!/usr/bin/env python3
"""
cch-eval — does compression preserve the answer?

Every other metric in this project measures VOLUME: bytes avoided, retrieval
ratio, break-even size. None of them can tell a win from a silent regression,
because none of them ask whether the information survived. This does.

Two layers:

  reachability (default, offline)
      For each fixture, plant a known needle in content of a shape taken from
      the real cache, stub it through cache-wrap, then try to recover the
      needle USING ONLY WHAT THE STUB ADVERTISES — its sections, its symbol
      menu, its line profile. A needle that cannot be recovered that way is
      information the compression layer destroyed in practice even though the
      bytes are still on disk. Requires no API key and belongs in CI.

  accuracy (--accuracy, needs an API key)
      Ask a model the question under two arms — full content, versus stub —
      and compare the answers. This is the arm that matches what other
      compression layers report. Raw HTTP over urllib, no SDK: the project
      is stdlib-only and an eval harness is a poor reason to change that.

Usage:
  cch-eval.py                 # reachability suite
  cch-eval.py --accuracy      # add the model A/B
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


def _credentials():
    """(headers, source) for the Messages API, or (None, reason).

    No SDK: the whole project is stdlib-only apart from an optional
    zstandard, and an eval harness is a poor reason to make `anthropic` the
    first hard dependency. Raw HTTP to /v1/messages is a dozen lines.
    """
    key = os.environ.get('ANTHROPIC_API_KEY')
    if key:
        return {'x-api-key': key}, 'ANTHROPIC_API_KEY'

    token = os.environ.get('ANTHROPIC_AUTH_TOKEN')
    if token:
        return ({'Authorization': f'Bearer {token}',
                 'anthropic-beta': 'oauth-2025-04-20'}, 'ANTHROPIC_AUTH_TOKEN')

    claude_creds = Path.home() / '.claude' / '.credentials.json'
    if claude_creds.exists():
        return None, (f'no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN set. '
                      f'{claude_creds} exists but belongs to Claude Code — '
                      f'export a key of your own rather than borrowing it')
    return None, 'no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN set'


def _ask(headers, model: str, prompt: str, max_tokens: int = 64):
    """One Messages API call. Returns (text, input_tokens)."""
    import urllib.error
    import urllib.request

    body = json.dumps({'model': model, 'max_tokens': max_tokens,
                       'messages': [{'role': 'user', 'content': prompt}]}).encode()
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages', data=body,
        headers={'content-type': 'application/json',
                 'anthropic-version': '2023-06-01', **headers})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    text = ''.join(b.get('text', '') for b in payload.get('content', [])
                   if b.get('type') == 'text')
    return text, (payload.get('usage') or {}).get('input_tokens')


def run_accuracy():
    """Model A/B: full content vs stub. Returns (results, skip_reason)."""
    headers, source = _credentials()
    if headers is None:
        return None, source

    model = os.environ.get('CCH_EVAL_MODEL', 'claude-haiku-4-5')
    question = ('The text above contains a canary token beginning "ZX9". '
                'Reply with that token and nothing else.')
    results = []
    for name, content, _term in _fixtures():
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            stub, _key = _stub(content, home, name)
            row = {'fixture': name, 'credential': source, 'model': model}
            for arm, payload in (('full', content), ('stub', stub)):
                try:
                    text, tokens = _ask(headers, model, f'{payload}\n\n{question}')
                    row[arm] = NEEDLE in text
                    row[f'{arm}_in'] = tokens
                except Exception as exc:
                    row[arm] = None
                    row[f'{arm}_error'] = str(exc)[:160]
            results.append(row)
    return results, None


def main() -> int:
    p = argparse.ArgumentParser(prog='cch-eval', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--accuracy', action='store_true',
                   help='also run the model A/B (needs an API key)')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    args = p.parse_args()

    reach = run_reachability()
    payload = {'reachability': reach}

    if args.accuracy:
        acc, skip = run_accuracy()
        payload['accuracy'] = acc
        payload['accuracy_skipped'] = skip

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(reach))
        if args.accuracy:
            print()
            if payload.get('accuracy_skipped'):
                print(f'Accuracy arm skipped: {payload["accuracy_skipped"]}')
            else:
                print('Accuracy — answer recovered under each arm')
                print('=' * 66)
                for r in payload['accuracy'] or []:
                    print(f'  {r["fixture"]:<20} full={r.get("full")}  '
                          f'stub={r.get("stub")}  '
                          f'tokens {r.get("full_in")} -> {r.get("stub_in")}')

    failed = [r for r in reach if not r['reachable']]
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
