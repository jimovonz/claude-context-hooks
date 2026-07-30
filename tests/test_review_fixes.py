"""Tests for the review fixes and the levers built on top of them.

Grouped by the defect or capability each one pins down, so a regression
names itself.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / 'hooks'
CCH_EDIT = HOOKS / 'cch-edit.py'
CCH_WRITE = HOOKS / 'cch-write.py'
CCH_BATCH = HOOKS / 'cch-batch.py'
CCH_HTML = HOOKS / 'cch-html.py'
CACHE_WRAP = HOOKS / 'cache-wrap.py'

sys.path.insert(0, str(HOOKS))


def _run(*argv, stdin=None, env=None, cwd=None):
    environ = os.environ.copy()
    if env:
        environ.update(env)
    p = subprocess.run([sys.executable, *[str(a) for a in argv]],
                       input=stdin.encode() if stdin else None,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=environ, cwd=cwd, timeout=60)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


# ---------------------------------------------------------------- symlinks

def test_cch_edit_follows_symlink_instead_of_replacing_it(tmp_path):
    """os.replace() over a symlink replaced the LINK and lost the edit.

    This repo installs its hooks as symlinks, so editing an installed path
    silently orphaned the change while printing a successful diff.
    """
    real = tmp_path / 'real.txt'
    real.write_text('alpha\n')
    link = tmp_path / 'link.txt'
    link.symlink_to(real)

    rc, _out, err = _run(CCH_EDIT, link, 'alpha', 'beta')
    assert rc == 0, err
    assert link.is_symlink(), 'symlink was replaced by a regular file'
    assert real.read_text() == 'beta\n', 'edit did not reach the real file'


def test_cch_write_follows_symlink(tmp_path):
    real = tmp_path / 'real.txt'
    real.write_text('old\n')
    link = tmp_path / 'link.txt'
    link.symlink_to(real)

    rc, _out, err = _run(CCH_WRITE, link, stdin='new\n')
    assert rc == 0, err
    assert link.is_symlink()
    assert real.read_text() == 'new\n'


def test_concurrent_writers_do_not_share_a_staging_file(tmp_path):
    """The fixed `<name>.cch-tmp` staging path raced across tool calls.

    The batch same-file guard only ever covered writers inside one batch;
    unique temp files remove the shared-staging failure mode entirely.
    """
    target = tmp_path / 'contended.txt'
    target.write_text('seed\n')
    results = []

    def writer(n):
        rc, _o, _e = _run(CCH_WRITE, target, stdin=f'{n}\n' * 500)
        results.append(rc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(rc == 0 for rc in results)
    body = target.read_text().splitlines()
    assert body, 'target was truncated by a lost update'
    assert len(set(body)) == 1, f'interleaved content from concurrent writers: {set(body)}'
    assert not list(tmp_path.glob('*.cch-tmp')), 'staging file left behind'


# ------------------------------------------------------------------ guards

def test_warning_prefix_is_not_shell_injectable():
    from lib import guards
    cmd = 'sed -n 1,200p $(id).py'
    warn = guards.warn_bulk_sed(cmd)
    assert warn and '$(id)' in warn
    rewritten = guards.prefix_warning('true', warn)
    assert 'echo "' not in rewritten
    out = subprocess.run(['bash', '-c', rewritten], stdout=subprocess.PIPE,
                         timeout=10).stdout.decode()
    assert '$(id).py' in out, 'command substitution was executed, not printed'
    assert 'uid=' not in out


def test_passthrough_matches_command_word_not_substring():
    from lib import guards
    assert guards.is_passthrough('ccm-get.py b2s:abc --head 5')
    assert guards.is_passthrough('cch-batch.py --jobs 2')
    # Merely NAMING one of our tools must not exempt a command.
    assert not guards.is_passthrough('rg -n cache-wrap.py hooks/')
    assert not guards.is_passthrough('cat notes-about-ccm-get.py.txt')


def test_bulk_read_checked_in_every_segment():
    from lib import guards
    assert guards.check_bulk_read('cat foo.py')
    assert guards.check_bulk_read('true && cat foo.py')
    assert guards.check_bulk_read('echo hi; cat foo.py')
    assert guards.check_bulk_read('grep x y.txt | cat foo.py')
    assert not guards.check_bulk_read('cat notes.txt')


def test_block_is_one_shot_overridable(tmp_path, monkeypatch):
    from lib import guards
    monkeypatch.setattr(guards, '_OVERRIDE_DIR', tmp_path / 'ov')
    cmd = 'cat some_module.py'
    first = guards.block(cmd, str(tmp_path))
    assert first and 'rerun' in first
    assert guards.block(cmd, str(tmp_path)) is None, 'rerun did not override'
    assert guards.block(cmd, str(tmp_path)), 'override was not one-shot'


def test_guards_apply_inside_cch_batch(tmp_path):
    (tmp_path / 'mod.py').write_text('x = 1\n' * 50)
    # Isolated HOME: the one-shot override marker must not leak between runs.
    rc, out, _err = _run(CCH_BATCH, stdin='cat mod.py\n', cwd=str(tmp_path),
                         env={'HOME': str(tmp_path)})
    assert rc == 0
    assert 'BLOCKED' in out, 'cch-batch bypassed the guard layer'


# ------------------------------------------------------------------ batching

def test_same_file_guard_resolves_cd_prefix_and_symlinks(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('cch_batch', CCH_BATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    real = tmp_path / 'f.txt'
    real.write_text('x\n')
    link = tmp_path / 'l.txt'
    link.symlink_to(real)

    direct = mod.target_file(f'cch-edit.py {real} a b')
    via_cd = mod.target_file(f'cd {tmp_path} && cch-edit.py f.txt a b')
    via_link = mod.target_file(f'cch-edit.py {link} a b')
    assert direct == via_cd == via_link == str(real.resolve())


# ------------------------------------------------------------------- budget

def test_budget_is_finite_and_denies_when_exhausted(tmp_path, monkeypatch):
    from lib import budget
    monkeypatch.setattr(budget, 'STATE_FILE', tmp_path / 'budget.json')
    monkeypatch.setattr(budget, 'BUDGET_TOKENS', 1000)

    granted, left, _ = budget.spend(600)
    assert granted and left == 400
    granted, left, _ = budget.spend(600)
    assert not granted, 'budget granted beyond its ceiling'
    granted, left, _ = budget.spend(400)
    assert granted and left == 0
    spent, total, _ = budget.status()
    assert (spent, total) == (1000, 1000)


def test_budget_survives_concurrent_spenders(tmp_path, monkeypatch):
    """The old lock-free read-modify-write lost grants under cch-batch."""
    from lib import budget
    monkeypatch.setattr(budget, 'STATE_FILE', tmp_path / 'budget.json')
    monkeypatch.setattr(budget, 'BUDGET_TOKENS', 10_000)

    granted = []

    def spender():
        ok, _left, _r = budget.spend(100)
        granted.append(ok)

    threads = [threading.Thread(target=spender) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spent, _total, _ = budget.status()
    assert spent == 100 * sum(granted), 'lost update: spend was not serialized'


def test_full_retrieval_spends_budget(tmp_path, monkeypatch):
    from lib import ccm_cache
    # The subprocess resolves the cache from HOME, so seed it in that layout.
    ccm_cache.init_ccm_cache(tmp_path / '.claude' / 'cache')
    key = ccm_cache.store_content('line\n' * 500)

    env = {'HOME': str(tmp_path), 'CCH_PASSTHROUGH_BUDGET': '25000'}
    rc, _out, err = _run(HOOKS / 'ccm-get.py', key, '--grep', '.',
                         '--reason', 'need the whole thing for this test case',
                         env=env)
    assert rc == 0
    assert 'budget:' in err, 'full retrieval was free'

    env['CCH_PASSTHROUGH_BUDGET'] = '1'
    rc, _out, err = _run(HOOKS / 'ccm-get.py', key, '--grep', '.',
                         '--reason', 'need the whole thing for this test case',
                         env=env)
    assert rc == 1 and 'budget' in err, 'exhausted budget still allowed full retrieval'


# -------------------------------------------------------------------- delta

def test_delta_collapses_identical_reread_and_diffs_changes(tmp_path):
    target = tmp_path / 'big.txt'
    target.write_text('\n'.join(f'line {i}' for i in range(1, 400)) + '\n')
    # Pin the threshold so the first read is inline regardless of the user's
    # settings.json — this test is about delta, not about caching.
    env = {'HOME': str(tmp_path), 'CCH_SESSION_ID': 'delta-test',
           'CCH_CACHE_THRESHOLD': '100000'}
    cmd = f'cat {target}'

    rc, first, _ = _run(CACHE_WRAP, '--', cmd, env=env)
    assert rc == 0 and 'line 399' in first

    rc, second, _ = _run(CACHE_WRAP, '--', cmd, env=env)
    assert 'CCM_UNCHANGED' in second
    assert len(second) < len(first) / 10

    target.write_text(target.read_text().replace('line 5\n', 'CHANGED\n'))
    rc, third, _ = _run(CACHE_WRAP, '--', cmd, env=env)
    assert 'CCM_DELTA' in third and 'CHANGED' in third
    assert 'line 300' not in third, 'diff emitted unchanged context'


def test_delta_is_session_scoped(tmp_path):
    target = tmp_path / 'big.txt'
    target.write_text('\n'.join(f'line {i}' for i in range(1, 400)) + '\n')
    cmd = f'cat {target}'
    base = {'HOME': str(tmp_path), 'CCH_CACHE_THRESHOLD': '100000'}
    _run(CACHE_WRAP, '--', cmd, env={**base, 'CCH_SESSION_ID': 'a'})
    _rc, out, _ = _run(CACHE_WRAP, '--', cmd, env={**base, 'CCH_SESSION_ID': 'b'})
    assert 'CCM_UNCHANGED' not in out, 'delta leaked across sessions'


# ------------------------------------------------------------ cache lifecycle

def test_prune_evicts_old_entries_but_never_pinned(tmp_path):
    from lib import ccm_cache
    ccm_cache.init_ccm_cache(tmp_path / 'cache')

    old_key = ccm_cache.store_content('old content\n' * 50)
    pinned_key = ccm_cache.store_content('pinned content\n' * 50,
                                         pin_level='hard', pin_reason='keep')
    fresh_key = ccm_cache.store_content('fresh content\n' * 50)

    stale = time.time() - 40 * 86400
    for key in (old_key, pinned_key):
        blob, _method = ccm_cache._find_blob_path(key)
        os.utime(blob, (stale, stale))

    result = ccm_cache.prune_cache(ttl_days=14)
    assert result['removed'] == 1
    assert ccm_cache.retrieve_content(old_key) is None
    assert ccm_cache.retrieve_content(pinned_key) is not None
    assert ccm_cache.retrieve_content(fresh_key) is not None


def test_metadata_is_rebuilt_when_sidecar_is_missing(tmp_path):
    from lib import ccm_cache
    ccm_cache.init_ccm_cache(tmp_path / 'cache')
    key = ccm_cache.store_content('content\n' * 20)
    ccm_cache._get_meta_path(key).unlink()
    assert ccm_cache.get_metadata(key) is None

    ccm_cache.store_content('content\n' * 20)     # same content, dedup path
    meta = ccm_cache.get_metadata(key)
    assert meta and meta.get('rebuilt') is True


# ----------------------------------------------------------------- cch-html

def test_html_refuses_non_http_schemes(tmp_path):
    page = tmp_path / 'p.html'
    page.write_text('<html><body><p>secret</p></body></html>')
    rc, out, err = _run(CCH_HTML, '--url', page.as_uri())
    assert rc != 0
    assert 'secret' not in out
    assert 'scheme' in err.lower()


def test_html_selector_no_match_is_a_failure_exit(tmp_path):
    rc, _out, err = _run(CCH_HTML, '--select', 'nosuchtag',
                         stdin='<html><body><p>hi</p></body></html>')
    assert rc == 2
    assert 'no match for selector' in err


def test_html_bounded_depth_does_not_blow_the_stack():
    deep = '<div>' * 5000 + 'deep text' + '</div>' * 5000
    rc, out, err = _run(CCH_HTML, stdin=f'<html><body>{deep}</body></html>')
    assert rc == 0, err
    assert 'deep text' in out


# --------------------------------------------------------- symbol-scoped edit

def test_cch_edit_symbol_replaces_a_graph_resolved_span(tmp_path):
    import sqlite3
    src = tmp_path / 'mod.py'
    src.write_text(
        'import os\n\n\n'
        'def target(a):\n'
        '    return a + 1\n\n\n'
        'def other():\n'
        '    return 2\n'
    )
    db_dir = tmp_path / '.code-review-graph'
    db_dir.mkdir()
    conn = sqlite3.connect(str(db_dir / 'graph.db'))
    conn.execute('CREATE TABLE nodes (kind TEXT, name TEXT, qualified_name TEXT, '
                 'file_path TEXT, line_start INT, line_end INT, language TEXT, '
                 'updated_at REAL)')
    conn.execute('CREATE TABLE edges (kind TEXT, source_qualified TEXT, '
                 'target_qualified TEXT, file_path TEXT, updated_at REAL)')
    conn.execute("INSERT INTO nodes VALUES ('Function', 'target', 'mod::target', "
                 "?, 4, 5, 'python', 0.0)", (str(src),))
    conn.commit()
    conn.close()

    body = tmp_path / 'body.py'
    body.write_text('def target(a, b):\n    return a + b\n')

    rc, out, err = _run(CCH_EDIT, src, '--symbol', 'target', '--new-file', body)
    assert rc == 0, err
    text = src.read_text()
    assert 'def target(a, b):' in text and 'return a + b' in text
    assert 'return a + 1' not in text
    assert 'def other():' in text and 'import os' in text
    assert 'replaced body of target' in out


def test_cch_edit_symbol_rejects_a_stale_graph(tmp_path):
    import sqlite3
    src = tmp_path / 'small.py'
    src.write_text('x = 1\n')
    db_dir = tmp_path / '.code-review-graph'
    db_dir.mkdir()
    conn = sqlite3.connect(str(db_dir / 'graph.db'))
    conn.execute('CREATE TABLE nodes (kind TEXT, name TEXT, qualified_name TEXT, '
                 'file_path TEXT, line_start INT, line_end INT, language TEXT, '
                 'updated_at REAL)')
    conn.execute('CREATE TABLE edges (kind TEXT, source_qualified TEXT, '
                 'target_qualified TEXT, file_path TEXT, updated_at REAL)')
    conn.execute("INSERT INTO nodes VALUES ('Function', 'gone', 'small::gone', "
                 "?, 40, 80, 'python', 0.0)", (str(src),))
    conn.commit()
    conn.close()

    body = tmp_path / 'body.py'
    body.write_text('pass\n')
    rc, _out, err = _run(CCH_EDIT, src, '--symbol', 'gone', '--new-file', body)
    assert rc == 1
    assert 'crg update' in err
    assert src.read_text() == 'x = 1\n', 'file was modified despite a stale span'


# ------------------------------------------------------------- event logging

def test_events_carry_a_session_id(tmp_path):
    from lib import event_log
    log = tmp_path / 'events.jsonl'
    original = event_log.EVENTS_LOG
    event_log.EVENTS_LOG = log
    try:
        os.environ['CCH_SESSION_ID'] = 'session-xyz'
        event_log.log_event('unit_test', detail='x')
    finally:
        event_log.EVENTS_LOG = original
        os.environ.pop('CCH_SESSION_ID', None)

    row = json.loads(log.read_text().strip())
    assert row['sid'] == 'session-xyz'
    assert row['event'] == 'unit_test'


def test_event_log_appends_survive_concurrency(tmp_path):
    from lib import event_log
    log = tmp_path / 'events.jsonl'
    original = event_log.EVENTS_LOG
    event_log.EVENTS_LOG = log
    try:
        threads = [threading.Thread(target=event_log.log_event,
                                    args=('unit_test',), kwargs={'i': i})
                   for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        event_log.EVENTS_LOG = original

    lines = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 50
    for line in lines:
        json.loads(line)      # every row is a whole, parseable object


# ---------------------------------------------------------------- installer

def test_installer_refuses_to_overwrite_unparseable_settings(tmp_path, monkeypatch):
    """'Starting fresh' would obliterate permissions, env and RTK's hooks."""
    sys.path.insert(0, str(REPO_ROOT))
    import install

    settings = tmp_path / 'settings.json'
    corrupt = '{ "permissions": [ "this file is not valid json"'
    settings.write_text(corrupt)
    monkeypatch.setattr(install, 'SETTINGS_FILE', settings)

    with pytest.raises(SystemExit) as exc:
        install.merge_settings()
    assert exc.value.code == 1
    assert settings.read_text() == corrupt, 'installer overwrote the file anyway'


# ------------------------------------------------------- wrapper resilience

def _isolated_wrapper(tmp_path, omit):
    """A copy of cache-wrap.py whose lib/ is missing `omit`."""
    import shutil
    hooks = tmp_path / 'hooks'
    (hooks / 'lib').mkdir(parents=True)
    shutil.copy(CACHE_WRAP, hooks / 'cache-wrap.py')
    for src in (HOOKS / 'lib').glob('*.py'):
        if src.name != omit:
            shutil.copy(src, hooks / 'lib' / src.name)
    return hooks / 'cache-wrap.py'


def test_missing_enrichment_lib_degrades_to_plain_caching(tmp_path):
    wrapper = _isolated_wrapper(tmp_path, omit='outline.py')
    rc, out, _err = _run(wrapper, '--', 'seq 1 4000',
                         env={'HOME': str(tmp_path), 'CCH_CACHE_THRESHOLD': '1000'})
    assert rc == 0
    assert 'CCM_CACHED' in out, 'caching should still work without the outline lib'
    assert 'sections:' not in out


def test_missing_cache_lib_still_delivers_output(tmp_path):
    """The failure that took the session down twice: output must survive."""
    wrapper = _isolated_wrapper(tmp_path, omit='ccm_cache.py')
    rc, out, _err = _run(wrapper, '--', 'seq 1 4000',
                         env={'HOME': str(tmp_path), 'CCH_CACHE_THRESHOLD': '1000'})
    assert rc == 0, 'wrapper must not die when the cache lib is unavailable'
    assert 'CCM_CACHED' not in out
    assert out.splitlines()[-1] == '4000', 'full output should pass through'


def test_wrapper_survives_every_single_lib_removal(tmp_path):
    """No individual lib file should be able to take Bash down."""
    for src in sorted((HOOKS / 'lib').glob('*.py')):
        if src.name == '__init__.py':
            continue
        case = tmp_path / src.stem
        case.mkdir()
        wrapper = _isolated_wrapper(case, omit=src.name)
        rc, out, err = _run(wrapper, '--', 'echo alive',
                            env={'HOME': str(case)})
        assert rc == 0, f'removing lib/{src.name} broke the wrapper: {err[:200]}'
        assert 'alive' in out, f'removing lib/{src.name} lost the output'


def test_bash_hook_survives_every_single_lib_removal(tmp_path):
    """intercept-bash gates every Bash call — it must never fail closed."""
    import shutil
    hook_src = HOOKS / 'intercept-bash.py'
    for src in sorted((HOOKS / 'lib').glob('*.py')):
        if src.name == '__init__.py':
            continue
        case = tmp_path / src.stem
        (case / 'lib').mkdir(parents=True)
        shutil.copy(hook_src, case / 'intercept-bash.py')
        for lib in (HOOKS / 'lib').glob('*.py'):
            if lib.name != src.name:
                shutil.copy(lib, case / 'lib' / lib.name)

        rc, out, err = _run(case / 'intercept-bash.py',
                            stdin=json.dumps({'tool_input': {'command': 'echo hi'}}),
                            env={'HOME': str(case)})
        assert rc == 0, f'removing lib/{src.name} broke the Bash hook: {err[:200]}'
        assert out.strip().startswith('{'), (
            f'removing lib/{src.name} produced no hook response: {out[:120]!r}')


# ------------------------------------------------------------ graph TESTED_BY

def _graph_with_tested_by(root, edges, nodes=()):
    import sqlite3
    d = root / '.code-review-graph'
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(d / 'graph.db'))
    conn.execute('CREATE TABLE nodes (kind TEXT, name TEXT, qualified_name TEXT, '
                 'file_path TEXT, line_start INT, line_end INT, language TEXT, '
                 'updated_at REAL)')
    conn.execute('CREATE TABLE edges (kind TEXT, source_qualified TEXT, '
                 'target_qualified TEXT, file_path TEXT, updated_at REAL)')
    for n in nodes:
        conn.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?,0.0)', n)
    for src, tgt in edges:
        conn.execute("INSERT INTO edges VALUES ('TESTED_BY',?,?,'',0.0)", (src, tgt))
    conn.commit(); conn.close()
    return d / 'graph.db'


def _graph_with_nodes(root, nodes):
    """graph.db carrying only definition nodes: (name, file, line_start, line_end)."""
    return _graph_with_tested_by(
        root, [],
        [('Function', name, f'{path}::{name}', path, a, b, 'python')
         for name, path, a, b in nodes])


def _add_calls_edge(db, caller, callee):
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO edges VALUES ('CALLS',?,?,'',0.0)",
                 (f'x.py::{caller}', f'x.py::{callee}'))
    conn.commit(); conn.close()


def test_tested_by_is_read_in_the_direction_it_is_written(tmp_path):
    """TESTED_BY is (source=symbol, target=test) — querying target found nothing.

    Every --tests lookup in the repo returned empty because the query looked
    in the wrong column.
    """
    from lib import guards
    db = _graph_with_tested_by(tmp_path, [
        ('generate_outline', f'{tmp_path}/tests/test_outline.py::test_sections'),
        ('generate_outline', f'{tmp_path}/tests/test_outline.py::test_profile'),
    ])
    answer = guards.graph_answer(db, 'tests', 'generate_outline')
    assert answer and 'test_sections' in answer and 'test_profile' in answer


def test_tested_by_matches_bare_source_names(tmp_path):
    """75% of TESTED_BY rows carry a bare name while nodes are path-qualified."""
    import sqlite3
    src = tmp_path / 'mod.py'
    src.write_text('def target(a):\n    return a\n')
    qname = f'{src}::target'
    _graph_with_tested_by(
        tmp_path,
        edges=[('target', f'{tmp_path}/tests/test_mod.py::test_target')],
        nodes=[
            ('Function', 'target', qname, str(src), 1, 2, 'python'),
            ('Test', 'test_target', f'{tmp_path}/tests/test_mod.py::test_target',
             f'{tmp_path}/tests/test_mod.py', 1, 3, 'python'),
        ],
    )
    body = tmp_path / 'body.py'
    body.write_text('def target(a):\n    return a + 1\n')
    rc, out, err = _run(CCH_EDIT, src, '--symbol', 'target', '--new-file', body)
    assert rc == 0, err
    assert 'tests:1' in out, f'bare-name TESTED_BY not matched: {out!r}'
    assert 'test_target' in out


# --- block misfires -----------------------------------------------------
# Three real misfires from the 2026-07-30 session. A false-positive BLOCK is
# expensive in a way a false-positive hint is not: it costs a whole turn to
# reroute, so the blocking tier needs precision the advisory tier does not.

def test_literal_flag_search_is_not_a_symbol_query(tmp_path):
    """grep "add_argument('--dist'" searches for text, not for a symbol.

    The callers pattern matched `SYMBOL(` followed by any quote, so a search
    for a call WITH a string argument read as "who calls add_argument" — and
    blocked a legitimate literal search the graph cannot serve.
    """
    from lib import guards
    db = _graph_with_nodes(tmp_path, [('add_argument', 'lib/x.py', 1, 2)])
    cmd = '''grep -n "add_argument('--dist'" hooks/cch-gain.py'''
    assert guards.check_symbol_grep(cmd, str(tmp_path)) is None


def test_who_calls_form_still_answers(tmp_path):
    """The intended shape — quote, symbol, paren, SAME quote — still blocks."""
    from lib import guards
    db = _graph_with_nodes(tmp_path, [('store_content', 'lib/x.py', 1, 9)])
    _add_calls_edge(db, 'main', 'store_content')
    assert guards.graph_answer(db, 'callers', 'store_content')


def test_ambiguous_symbol_does_not_block(tmp_path):
    """A symbol defined in many places has no single answer.

    log_event resolves to 11 definitions (a fallback stub per hook). A
    truncated 3-of-11 list reads as if it were the answer, so ambiguity is a
    reason to let the grep run.
    """
    from lib import guards
    db = _graph_with_nodes(tmp_path, [
        ('log_event', f'lib/h{i}.py', 1, 2) for i in range(5)])
    assert guards.graph_answer(db, 'location', 'log_event') is None


def test_unique_symbol_still_answers(tmp_path):
    from lib import guards
    db = _graph_with_nodes(tmp_path, [('_find_blob_path', 'lib/ccm.py', 211, 220)])
    answer = guards.graph_answer(db, 'location', '_find_blob_path')
    assert answer and '211-220' in answer


def test_library_hub_callers_are_not_served(tmp_path):
    """A call with no local definition is a library hub, not the graph's business.

    add_argument has 86x fan-in and its caller list reads "main · main · main"
    — noise that blocked a real search for nothing.
    """
    from lib import guards
    db = _graph_with_nodes(tmp_path, [])          # no definition node
    _add_calls_edge(db, 'main', 'add_argument')
    assert guards.graph_answer(db, 'callers', 'add_argument') is None


def test_batch_invocations_delegate_guards_to_their_own_lines():
    """The hook must not guard a cch-batch heredoc as one command.

    Every batched line lives inside the heredoc, so guarding the string
    denied the whole call over one line and the other lines never ran —
    defeating cch-batch's per-line guard, which blocks just the offending
    line and leaves the rest running.
    """
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        'ib', Path(__file__).resolve().parent.parent / 'hooks' / 'intercept-bash.py')
    ib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ib)
    batch = ("cch-batch.py << 'EOF'\n"
             "grep -n 'def target_fn' src/x.py\n"
             "git status -sb\n"
             "EOF")
    assert ib._is_batch(batch) is True
    assert ib._is_batch('git status -sb') is False
    assert ib._is_batch('rg -n foo src/ | cch-batch.py') is True
