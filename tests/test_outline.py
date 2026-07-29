"""Tests for the extractive outline and ccm-get --chars."""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / 'hooks'
CACHE_WRAP = HOOKS / 'cache-wrap.py'
CCM_GET = HOOKS / 'ccm-get.py'

sys.path.insert(0, str(HOOKS))

from lib.outline import generate_outline


def _run(*argv, env=None):
    environ = os.environ.copy()
    if env:
        environ.update(env)
    p = subprocess.run([sys.executable, *[str(a) for a in argv]],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=environ, timeout=60)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


# ------------------------------------------------------------------ sections

def test_sections_from_producer_delimiters():
    """Composite shell output labels its own sections — the cheapest index."""
    content = (
        '=========== prompt_hook ===========\n'
        + 'body\n' * 40
        + '=========== stop_hook ===========\n'
        + 'body\n' * 40
        + '=========== posttool_hook ===========\n'
        + 'body\n' * 40
    )
    out = generate_outline(content)
    assert 'sections: 3' in out
    assert 'L1 prompt_hook' in out
    assert 'L42 stop_hook' in out
    assert 'L83 posttool_hook' in out


def test_bare_banner_takes_label_from_next_line():
    content = ('################\nagent-loop.ts\n' + 'x\n' * 20) * 2
    out = generate_outline(content)
    assert 'sections: 2' in out
    assert 'agent-loop.ts' in out


def test_single_banner_is_not_a_section_index():
    content = '==== only one ====\n' + 'body\n' * 50
    out = generate_outline(content)
    assert 'sections:' not in out
    assert 'lines:' in out


def test_dominant_delimiter_wins():
    content = (
        '#### alpha ####\nx\n#### beta ####\ny\n#### gamma ####\nz\n'
        '**** stray ****\nq\n'
    )
    out = generate_outline(content)
    assert 'sections: 3' in out
    assert 'alpha' in out and 'gamma' in out
    assert 'stray' not in out


# ------------------------------------------------------------------- profile

def test_profile_reports_line_shape():
    out = generate_outline('abc\n' * 10)
    assert 'lines: 10' in out
    assert 'median 3 chars' in out
    assert 'max 3' in out


def test_long_line_outlier_is_flagged_with_chars_hint():
    """A line count alone hides a 20k-char line; --lines on it is a trap."""
    content = 'short\nshort\n' + 'x' * 20469 + '\nshort\n'
    out = generate_outline(content)
    assert 'max 20469' in out
    assert 'L3 is 20,469 chars' in out
    assert '--chars' in out


def test_outline_is_extractive_only():
    """Every label must be text copied from the content, never invented."""
    content = ('=== alpha ===\n' + 'body\n' * 30) * 2
    out = generate_outline(content)
    for token in out.split():
        if token.startswith(('L', 'sections:', 'lines:', 'median', 'max', '·')):
            continue
        assert token in content or token.isdigit() or token in ('chars',), token


def test_outline_survives_junk():
    assert generate_outline('') is None
    assert generate_outline('\x00\x01 binary-ish') is not None


# --------------------------------------------------------------- integration

def test_outline_appears_in_stub(tmp_path):
    target = tmp_path / 'composite.txt'
    target.write_text(
        ('=========== section_one ===========\n' + 'padding line\n' * 300)
        + ('=========== section_two ===========\n' + 'padding line\n' * 300)
    )
    rc, out, _err = _run(CACHE_WRAP, '--', f'cat {target}',
                         env={'HOME': str(tmp_path), 'CCH_CACHE_THRESHOLD': '1000'})
    assert rc == 0
    assert 'CCM_CACHED' in out
    assert 'sections: 2' in out
    assert 'section_one' in out and 'section_two' in out
    assert 'lines:' in out and 'median' in out
    assert '--chars A-B' in out


# ------------------------------------------------------------------- --chars

def _seed(tmp_path, content):
    from lib import ccm_cache
    ccm_cache.init_ccm_cache(tmp_path / '.claude' / 'cache')
    return ccm_cache.store_content(content)


def test_chars_slices_a_long_line(tmp_path):
    """The failure this fixes: a 7-line blob with one 20k line was unsliceable."""
    key = _seed(tmp_path, 'head\n' + ('A' * 5000 + 'NEEDLE' + 'B' * 5000) + '\ntail\n')
    rc, out, err = _run(CCM_GET, key, '--chars', '5006-5011',
                        env={'HOME': str(tmp_path)})
    assert rc == 0, err
    assert out.strip() == 'NEEDLE'
    assert 'chars]' in err


def test_chars_counts_as_a_filter(tmp_path):
    key = _seed(tmp_path, 'x' * 4000)
    rc, _out, err = _run(CCM_GET, key, '--chars', '1-10', env={'HOME': str(tmp_path)})
    assert rc == 0
    assert 'At least one filter is required' not in err


def test_chars_open_ended_and_invalid(tmp_path):
    key = _seed(tmp_path, 'abcdefghij')
    rc, out, _ = _run(CCM_GET, key, '--chars', '8-', env={'HOME': str(tmp_path)})
    assert rc == 0 and out.strip() == 'hij'

    rc, _out, err = _run(CCM_GET, key, '--chars', 'nope', env={'HOME': str(tmp_path)})
    assert rc == 1 and 'Invalid character range' in err


# --------------------------------------------------------- instrumentation

def test_stub_event_records_outline_shape(tmp_path):
    """Without these fields the index cannot be evaluated after the fact."""
    target = tmp_path / 'delimited.txt'
    target.write_text(
        ('==== alpha ====\n' + 'padding\n' * 400)
        + ('==== beta ====\n' + 'padding\n' * 400)
    )
    rc, _out, _err = _run(CACHE_WRAP, '--', f'cat {target}',
                          env={'HOME': str(tmp_path), 'CCH_CACHE_THRESHOLD': '1000',
                               'CCH_SESSION_ID': 'sess-1'})
    assert rc == 0
    log = tmp_path / '.claude' / 'cache' / 'ccm' / 'events.jsonl'
    rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    cached = [r for r in rows if r.get('event') == 'cache_wrap' and r.get('cached')]
    assert cached, 'no cached event logged'
    assert cached[-1]['outline_sections'] == 2
    assert cached[-1]['outline_bytes'] > 0
    assert cached[-1]['has_menu'] is False
    assert cached[-1]['sid'] == 'sess-1'


def test_retrieval_log_records_session(tmp_path):
    key = _seed(tmp_path, 'line\n' * 900)
    rc, _out, _err = _run(CCM_GET, key, '--head', '5',
                          env={'HOME': str(tmp_path), 'CCH_SESSION_ID': 'sess-2'})
    assert rc == 0
    log = tmp_path / '.claude' / 'cache' / 'ccm' / 'retrieval.log'
    row = [json.loads(x) for x in log.read_text().splitlines() if x.strip()][-1]
    assert row['sid'] == 'sess-2'


def test_budget_denial_is_logged(tmp_path, monkeypatch):
    """A denied grant is the signal the budget is binding — it must be visible."""
    from lib import budget, event_log
    monkeypatch.setattr(budget, 'STATE_FILE', tmp_path / 'b.json')
    monkeypatch.setattr(budget, 'BUDGET_TOKENS', 100)
    monkeypatch.setattr(event_log, 'EVENTS_LOG', tmp_path / 'ev.jsonl')

    assert budget.spend(80, kind='passthrough')[0] is True
    assert budget.spend(80, kind='passthrough')[0] is False

    rows = [json.loads(x) for x in (tmp_path / 'ev.jsonl').read_text().splitlines() if x.strip()]
    kinds = [r['event'] for r in rows]
    assert 'budget_spend' in kinds and 'budget_denied' in kinds
    denied = [r for r in rows if r['event'] == 'budget_denied'][0]
    assert denied['wanted'] == 80 and denied['remaining'] == 20
