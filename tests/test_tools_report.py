"""cch-gain --tools — graph-first%, the tool-choice metric.

The denominator is deliberately "navigation the graph could have served"
(cairn-graph + symbol-shaped search + code file read), not all activity, so
the number measures tool CHOICE rather than how busy a session was.
"""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'cch_gain', Path(__file__).resolve().parent.parent / 'hooks' / 'cch-gain.py')
cch_gain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cch_gain)


def test_graph_commands_classify_as_graph():
    assert cch_gain._nav_kind('cairn-graph --location handle_request') == 'graph'
    assert cch_gain._nav_kind('cairn-graph --callers foo') == 'graph'


def test_symbol_shaped_search_classifies_as_search():
    assert cch_gain._nav_kind('rg -n handle_request src/') == 'search'
    assert cch_gain._nav_kind("grep -rn 'handle_request' src/") == 'search'


def test_code_file_read_classifies_as_read():
    assert cch_gain._nav_kind("sed -n '10,40p' src/server.py") == 'read'
    assert cch_gain._nav_kind('cat hooks/lib/guards.py') == 'read'


def test_non_navigation_yields_none():
    assert cch_gain._nav_kind('git status') is None
    assert cch_gain._nav_kind('pytest -q') is None
    # Reading a non-code file is not code navigation.
    assert cch_gain._nav_kind('cat README.md') is None


def test_trailing_pipe_filter_does_not_count_as_navigation():
    # The act is the first segment; a trailing grep narrows its output.
    assert cch_gain._nav_kind('ip addr show eth0 | grep inet') is None
    assert cch_gain._nav_kind('cairn-graph --summary | head -5') == 'graph'


def test_render_reports_rate_and_per_session_rows(tmp_path, monkeypatch):
    log = tmp_path / 'events.jsonl'
    now = datetime.now().isoformat(timespec='seconds')
    rows = []
    # one session that uses the graph, one that never does
    for _ in range(2):
        rows.append(f'{{"ts":"{now}","event":"cache_wrap","sid":"good","cmd_head":"cairn-graph --location f"}}')
    for _ in range(4):
        rows.append(f'{{"ts":"{now}","event":"cache_wrap","sid":"good","cmd_head":"rg -n handle_request src/"}}')
    for _ in range(6):
        rows.append(f'{{"ts":"{now}","event":"cache_wrap","sid":"bad","cmd_head":"cat src/server.py"}}')
    log.write_text('\n'.join(rows) + '\n')
    monkeypatch.setattr(cch_gain, 'EVENTS_LOG', log)

    out = cch_gain.render_tools(datetime.now() - timedelta(days=1), 1)
    assert 'GRAPH-FIRST: 16.7%' in out       # 2 graph of 12 total navigation
    assert 'good' in out and 'bad' in out
    assert 'sessions at 0%: 1/2' in out


def test_render_handles_an_empty_window(tmp_path, monkeypatch):
    log = tmp_path / 'events.jsonl'
    log.write_text('')
    monkeypatch.setattr(cch_gain, 'EVENTS_LOG', log)
    assert 'No navigation commands' in cch_gain.render_tools(datetime.now(), 1)
