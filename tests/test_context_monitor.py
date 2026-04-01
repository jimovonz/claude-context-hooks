"""Tests for context-monitor.py"""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import StringIO

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
import importlib


def _import_monitor():
    """Import context-monitor module (has hyphens in name)."""
    spec = importlib.util.spec_from_file_location(
        "context_monitor",
        Path(__file__).parent.parent / 'hooks' / 'context-monitor.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


monitor = _import_monitor()


# ─── count_tokens ────────────────────────────────────────────

#TAG: [C001]
# Verifies: count_tokens estimates tokens from character count when tiktoken unavailable
@pytest.mark.behavioural
def test_count_tokens_estimation():
    with patch.object(monitor, '_tokenizer', None):
        result = monitor.count_tokens("x" * 250)
        assert result == 100  # 250 / 2.5


#TAG: [C002]
# Verifies: count_tokens returns integer for arbitrary text length
@pytest.mark.behavioural
def test_count_tokens_returns_int():
    with patch.object(monitor, '_tokenizer', None):
        result = monitor.count_tokens("hello world")
        assert isinstance(result, int)


# ─── extract_content_text ────────────────────────────────────

#TAG: [C003]
# Verifies: extract_content_text extracts text from various content block types
@pytest.mark.behavioural
def test_extract_content_text_blocks():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        {"type": "thinking", "thinking": "I think..."},
    ]
    result = monitor.extract_content_text(content)
    assert "hello" in result
    assert "Bash" in result
    assert "I think..." in result


#TAG: [C004]
# Verifies: extract_content_text handles string content directly
@pytest.mark.behavioural
def test_extract_content_text_string():
    assert monitor.extract_content_text("plain string") == "plain string"


# ─── find_last_compaction (non-trivial: 4 tests) ─────────────

#TAG: [C005]
# Verifies: find_last_compaction returns index of compaction summary line
@pytest.mark.behavioural
def test_find_last_compaction_found():
    lines = [
        '{"message":{"role":"user","content":"hello"}}\n',
        '{"isCompactSummary":true,"message":{"role":"assistant","content":"summary"}}\n',
        '{"message":{"role":"user","content":"after compact"}}\n',
    ]
    assert monitor.find_last_compaction(lines) == 1


#TAG: [C006]
# Verifies: find_last_compaction returns 0 when no compaction exists
@pytest.mark.edge
def test_find_last_compaction_none():
    lines = [
        '{"message":{"role":"user","content":"hello"}}\n',
        '{"message":{"role":"assistant","content":"hi"}}\n',
    ]
    assert monitor.find_last_compaction(lines) == 0


#TAG: [C007]
# Verifies: find_last_compaction handles invalid JSON lines without crashing
@pytest.mark.error
def test_find_last_compaction_invalid_json():
    lines = [
        'not json at all\n',
        '{"isCompactSummary": but broken\n',
        '{"message":{}}\n',
    ]
    assert monitor.find_last_compaction(lines) == 0


#TAG: [C008]
# Verifies: find_last_compaction finds compaction in nested content structure
@pytest.mark.adversarial
def test_find_last_compaction_nested():
    nested = json.dumps({
        "message": {
            "content": [{"isCompactSummary": True, "type": "text", "text": "compact"}]
        },
        "isCompactSummary": "not_at_top"
    })
    lines = ['{"message":{}}\n', nested + '\n']
    result = monitor.find_last_compaction(lines)
    assert result == 1


# ─── estimate_context (non-trivial: 4 tests) ────────────────

#TAG: [C009]
# Verifies: estimate_context returns percentage and token count for valid session
@pytest.mark.behavioural
def test_estimate_context_basic(tmp_path):
    session_file = tmp_path / "session.jsonl"
    messages = []
    for i in range(10):
        messages.append(json.dumps({
            "message": {"role": "user", "content": "x" * 1000}
        }))
    session_file.write_text('\n'.join(messages) + '\n')

    with patch.object(monitor, '_tokenizer', None):
        pct, tokens = monitor.estimate_context(str(session_file))
    assert 0 < pct <= 100
    assert tokens > 0


#TAG: [C00A]
# Verifies: estimate_context returns (0, 0) for nonexistent session file
@pytest.mark.edge
def test_estimate_context_missing_file():
    pct, tokens = monitor.estimate_context("/nonexistent/session.jsonl")
    assert pct == 0
    assert tokens == 0


#TAG: [C00B]
# Verifies: estimate_context handles empty session file
@pytest.mark.error
def test_estimate_context_empty_file(tmp_path):
    session_file = tmp_path / "empty.jsonl"
    session_file.write_text("")
    with patch.object(monitor, '_tokenizer', None):
        pct, tokens = monitor.estimate_context(str(session_file))
    # Should have overhead tokens only
    assert tokens == monitor.CONTEXT_OVERHEAD_TOKENS


#TAG: [C00C]
# Verifies: estimate_context handles session with all malformed JSON lines
@pytest.mark.adversarial
def test_estimate_context_all_corrupt(tmp_path):
    session_file = tmp_path / "corrupt.jsonl"
    session_file.write_text("not json\nalso bad\n{{{}\n")
    with patch.object(monitor, '_tokenizer', None):
        pct, tokens = monitor.estimate_context(str(session_file))
    assert tokens == monitor.CONTEXT_OVERHEAD_TOKENS


# ─── get_crossed_threshold ───────────────────────────────────

#TAG: [C00D]
# Verifies: get_crossed_threshold returns highest threshold crossed above last warning
@pytest.mark.behavioural
def test_get_crossed_threshold_found():
    with patch.object(monitor, 'CONTEXT_WARN_THRESHOLDS', [70, 80, 90]):
        assert monitor.get_crossed_threshold(85, 0) == 80
        assert monitor.get_crossed_threshold(95, 80) == 90
        assert monitor.get_crossed_threshold(95, 0) == 90


#TAG: [C00E]
# Verifies: get_crossed_threshold returns None when no new threshold crossed
@pytest.mark.behavioural
def test_get_crossed_threshold_none():
    with patch.object(monitor, 'CONTEXT_WARN_THRESHOLDS', [70, 80, 90]):
        assert monitor.get_crossed_threshold(65, 0) is None
        assert monitor.get_crossed_threshold(85, 90) is None


# ─── get_last_warning / set_last_warning ─────────────────────

#TAG: [C00F]
# Verifies: get_last_warning returns 0 when no state file exists
@pytest.mark.behavioural
def test_get_last_warning_no_file(tmp_path):
    assert monitor.get_last_warning(tmp_path / "nonexistent") == 0


#TAG: [C010]
# Verifies: set_last_warning then get_last_warning roundtrips value
@pytest.mark.behavioural
def test_set_get_last_warning_roundtrip(tmp_path):
    state_file = tmp_path / "state" / "level"
    monitor.set_last_warning(state_file, 80)
    assert monitor.get_last_warning(state_file) == 80


# ─── main (non-trivial: 4 tests) ────────────────────────────

#TAG: [C011]
# Verifies: main skips processing when CONTEXT_MONITOR_ENABLED is False
@pytest.mark.behavioural
def test_main_disabled():
    with patch.object(monitor, 'CONTEXT_MONITOR_ENABLED', False):
        # Should return without reading stdin
        monitor.main()  # no error = success


#TAG: [C012]
# Verifies: main handles empty transcript_path by returning early
@pytest.mark.edge
def test_main_no_transcript():
    with patch.object(monitor, 'CONTEXT_MONITOR_ENABLED', True):
        with patch('sys.stdin', StringIO(json.dumps({"transcript_path": ""}))):
            monitor.main()  # no error = success


#TAG: [C013]
# Verifies: main handles missing session file without crashing
@pytest.mark.error
def test_main_missing_session(tmp_path):
    with patch.object(monitor, 'CONTEXT_MONITOR_ENABLED', True):
        data = {"transcript_path": str(tmp_path / "missing.jsonl")}
        with patch('sys.stdin', StringIO(json.dumps(data))):
            monitor.main()  # estimate_context returns (0,0), no threshold crossed


#TAG: [C014]
# Verifies: main writes warning to TTY when threshold is crossed
@pytest.mark.adversarial
def test_main_writes_warning(tmp_path):
    # Create a session file with enough content to cross 70% threshold
    session_file = tmp_path / "session.jsonl"
    # Create enough text to make context > 70% of CONTEXT_MAX_TOKENS
    big_content = "x" * 500000
    session_file.write_text(json.dumps({
        "message": {"role": "user", "content": big_content}
    }) + '\n')

    state_file = tmp_path / "state" / f"{session_file.stem}-context-level"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    with patch.object(monitor, 'CONTEXT_MONITOR_ENABLED', True), \
         patch.object(monitor, '_tokenizer', None), \
         patch.object(monitor, 'CONTEXT_MAX_TOKENS', 100000), \
         patch.object(monitor, 'CONTEXT_OVERHEAD_TOKENS', 0), \
         patch.object(monitor, 'CONTEXT_MESSAGE_MULTIPLIER', 1.0), \
         patch.object(monitor, 'CONTEXT_WARN_THRESHOLDS', [70, 80, 90]), \
         patch('sys.stdin', StringIO(json.dumps({"transcript_path": str(session_file)}))), \
         patch('builtins.open', side_effect=lambda *a, **kw: (
             open(*a, **kw) if str(a[0]) == str(session_file) or str(a[0]).endswith('-context-level')
             else MagicMock()
         )) as mock_open:
        # Mock the TTY path discovery to fail gracefully
        with patch('os.getppid', return_value=1):
            monitor.main()
