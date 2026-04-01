"""Shared fixtures for claude-context-hooks tests."""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

# Add hooks directory to path so we can import modules
HOOKS_DIR = Path(__file__).parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(HOOKS_DIR.parent))


@pytest.fixture
def tmp_cache(tmp_path):
    """Provide a temporary cache directory and initialize CCM."""
    from lib.ccm_cache import init_ccm_cache
    init_ccm_cache(tmp_path)
    return tmp_path


@pytest.fixture
def tmp_session(tmp_path):
    """Provide a temporary session transcript file."""
    session_file = tmp_path / "test-session.jsonl"
    session_file.touch()
    return session_file


@pytest.fixture
def hook_input_factory():
    """Factory for creating hook input dicts."""
    def _make(tool_name, tool_input=None, transcript_path="", tool_use_id="test-id-123", cwd="/tmp"):
        return {
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "transcript_path": transcript_path,
            "tool_use_id": tool_use_id,
            "session": {"cwd": cwd},
        }
    return _make


@pytest.fixture(autouse=True)
def isolate_ccm_globals():
    """Reset CCM cache globals between tests."""
    import lib.ccm_cache as ccm
    old_values = {
        'CCM_CACHE_DIR': ccm.CCM_CACHE_DIR,
        'CCM_BLOBS_DIR': ccm.CCM_BLOBS_DIR,
        'CCM_META_DIR': ccm.CCM_META_DIR,
        'CCM_INDEX_FILE': ccm.CCM_INDEX_FILE,
        'CCM_LAST_KEY_FILE': ccm.CCM_LAST_KEY_FILE,
    }
    yield
    for k, v in old_values.items():
        setattr(ccm, k, v)


@pytest.fixture
def mock_stdin():
    """Helper to mock sys.stdin with JSON data."""
    import io
    def _mock(data):
        return io.StringIO(json.dumps(data))
    return _mock
