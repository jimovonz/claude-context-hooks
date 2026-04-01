"""Tests for ccm-get.py"""

import json
import sys
import argparse
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock
from io import StringIO

sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
import importlib

from lib.ccm_cache import init_ccm_cache, store_content, get_metadata


def _import_ccm_get():
    spec = importlib.util.spec_from_file_location(
        "ccm_get",
        Path(__file__).parent.parent / 'hooks' / 'ccm-get.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ccm_get = _import_ccm_get()


# ─── log_retrieval (non-trivial: 4 tests) ────────────────────

#TAG: [I001]
# Verifies: log_retrieval writes valid JSON entry with filter details to log file
@pytest.mark.behavioural
def test_log_retrieval_writes_entry(tmp_cache, tmp_path):
    key = store_content("test content\n" * 100)
    log_file = tmp_path / "retrieval.log"

    args = argparse.Namespace(
        grep="error",
        head=None,
        tail=None,
        lines=None,
        context=None,
        reason=None,
    )
    with patch.object(ccm_get, 'RETRIEVAL_LOG', log_file):
        ccm_get.log_retrieval(key, args, returned_bytes=500)

    entry = json.loads(log_file.read_text().strip())
    assert entry['filter']['grep'] == 'error'
    assert entry['returned_bytes'] == 500
    assert entry['is_full_retrieval'] is False
    assert 'timestamp' in entry


#TAG: [I002]
# Verifies: log_retrieval detects full retrieval patterns like "." and ".*"
@pytest.mark.edge
def test_log_retrieval_full_retrieval_patterns(tmp_cache, tmp_path):
    key = store_content("data\n" * 100)
    log_file = tmp_path / "retrieval.log"

    for pattern in ['.', '.*', '^', '.*$', '^.*$']:
        args = argparse.Namespace(
            grep=pattern, head=None, tail=None, lines=None,
            context=None, reason="need full content for editing"
        )
        with patch.object(ccm_get, 'RETRIEVAL_LOG', log_file):
            ccm_get.log_retrieval(key, args, returned_bytes=1000)
        entries = [json.loads(l) for l in log_file.read_text().strip().split('\n')]
        assert entries[-1]['is_full_retrieval'] is True


#TAG: [I003]
# Verifies: log_retrieval handles missing metadata gracefully
@pytest.mark.error
def test_log_retrieval_missing_metadata(tmp_cache, tmp_path):
    log_file = tmp_path / "retrieval.log"
    args = argparse.Namespace(
        grep="test", head=None, tail=None, lines=None,
        context=None, reason=None,
    )
    with patch.object(ccm_get, 'RETRIEVAL_LOG', log_file):
        ccm_get.log_retrieval("b2s:nonexistent00000", args, returned_bytes=0)
    entry = json.loads(log_file.read_text().strip())
    assert entry['source_tool'] is None
    assert entry['savings_pct'] is None


#TAG: [I004]
# Verifies: log_retrieval does not crash when log file is unwritable
@pytest.mark.adversarial
def test_log_retrieval_unwritable(tmp_cache, tmp_path):
    args = argparse.Namespace(
        grep="test", head=None, tail=None, lines=None,
        context=None, reason=None,
    )
    with patch.object(ccm_get, 'RETRIEVAL_LOG', tmp_path / "nonexistent_dir" / "log"):
        # Should not raise - swallows exceptions
        ccm_get.log_retrieval("b2s:abc", args, returned_bytes=0)


# ─── main CLI filters ───────────────────────────────────────

#TAG: [I005]
# Verifies: main --head N returns only the first N lines of cached content
@pytest.mark.behavioural
def test_main_head_filter(tmp_cache, capsys):
    content = '\n'.join(f"line {i}" for i in range(100))
    key = store_content(content)

    with patch('sys.argv', ['ccm-get.py', key, '--head', '5']), \
         patch.object(ccm_get, 'init_ccm_cache', return_value=None), \
         patch.object(ccm_get, 'RETRIEVAL_LOG', Path('/dev/null')):
        ccm_get.main()
    out = capsys.readouterr().out
    lines = out.strip().split('\n')
    assert len(lines) == 5
    assert lines[0] == "line 0"
    assert lines[4] == "line 4"


#TAG: [I006]
# Verifies: main --grep filters lines matching regex pattern
@pytest.mark.behavioural
def test_main_grep_filter(tmp_cache, capsys):
    content = "apple\nbanana\napricot\ncherry\n"
    key = store_content(content)

    with patch('sys.argv', ['ccm-get.py', key, '--grep', '^ap']), \
         patch.object(ccm_get, 'init_ccm_cache', return_value=None), \
         patch.object(ccm_get, 'RETRIEVAL_LOG', Path('/dev/null')):
        ccm_get.main()
    out = capsys.readouterr().out
    lines = out.strip().split('\n')
    assert len(lines) == 2
    assert "apple" in lines
    assert "apricot" in lines


#TAG: [I007]
# Verifies: main exits with error when no filter is provided
@pytest.mark.behavioural
def test_main_no_filter_error(tmp_cache):
    key = store_content("data")
    with patch('sys.argv', ['ccm-get.py', key]):
        with pytest.raises(SystemExit) as exc_info:
            ccm_get.main()
        assert exc_info.value.code == 1


#TAG: [I008]
# Verifies: main --grep "." without --reason exits with error
@pytest.mark.behavioural
def test_main_full_retrieval_requires_reason(tmp_cache):
    key = store_content("data")
    with patch('sys.argv', ['ccm-get.py', key, '--grep', '.']):
        with pytest.raises(SystemExit) as exc_info:
            ccm_get.main()
        assert exc_info.value.code == 1


#TAG: [I009]
# Verifies: main --lines range returns correct subset of content
@pytest.mark.behavioural
def test_main_lines_filter(tmp_cache, capsys):
    content = '\n'.join(f"line {i}" for i in range(50))
    key = store_content(content)

    with patch('sys.argv', ['ccm-get.py', key, '--lines', '10-12']), \
         patch.object(ccm_get, 'init_ccm_cache', return_value=None), \
         patch.object(ccm_get, 'RETRIEVAL_LOG', Path('/dev/null')):
        ccm_get.main()
    out = capsys.readouterr().out
    lines = out.strip().split('\n')
    assert len(lines) == 3
    assert lines[0] == "line 9"   # 1-indexed: line 10 = index 9
    assert lines[2] == "line 11"
