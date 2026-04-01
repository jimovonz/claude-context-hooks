"""Tests for intercept-webfetch.py"""

import json
import sys
import urllib.error
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import StringIO

sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
import importlib


def _import_webfetch():
    spec = importlib.util.spec_from_file_location(
        "intercept_webfetch",
        Path(__file__).parent.parent / 'hooks' / 'intercept-webfetch.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


webfetch_mod = _import_webfetch()


# ─── fetch_url (non-trivial: 4 tests) ───────────────────────

#TAG: [G001]
# Verifies: fetch_url returns content and status 200 for successful fetch
@pytest.mark.behavioural
def test_fetch_url_success():
    mock_response = MagicMock()
    mock_response.read.return_value = b"<html>Hello</html>"
    mock_response.status = 200
    mock_response.headers.get_content_charset.return_value = 'utf-8'
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch('urllib.request.urlopen', return_value=mock_response):
        content, status, error = webfetch_mod.fetch_url("http://example.com")
        assert content == "<html>Hello</html>"
        assert status == 200
        assert error == ''


#TAG: [G002]
# Verifies: fetch_url handles charset None by falling back to utf-8
@pytest.mark.edge
def test_fetch_url_no_charset():
    mock_response = MagicMock()
    mock_response.read.return_value = "unicode text".encode('utf-8')
    mock_response.status = 200
    mock_response.headers.get_content_charset.return_value = None
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch('urllib.request.urlopen', return_value=mock_response):
        content, status, error = webfetch_mod.fetch_url("http://example.com")
        assert content == "unicode text"
        assert error == ''


#TAG: [G003]
# Verifies: fetch_url returns HTTP error code and message on HTTPError
@pytest.mark.error
def test_fetch_url_http_error():
    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(
        "http://example.com", 404, "Not Found", {}, None
    )):
        content, status, error = webfetch_mod.fetch_url("http://example.com")
        assert content == ''
        assert status == 404
        assert "404" in error


#TAG: [G004]
# Verifies: fetch_url handles UnicodeDecodeError with latin-1 fallback
@pytest.mark.adversarial
def test_fetch_url_encoding_fallback():
    # Content that fails utf-8 but succeeds with latin-1
    raw_bytes = bytes(range(128, 256))  # high bytes invalid for utf-8

    mock_response = MagicMock()
    mock_response.read.return_value = raw_bytes
    mock_response.status = 200
    mock_response.headers.get_content_charset.return_value = 'utf-8'
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch('urllib.request.urlopen', return_value=mock_response):
        content, status, error = webfetch_mod.fetch_url("http://example.com")
        # Should get some decoded content (via utf-8 with replace or latin-1)
        assert len(content) > 0
        assert error == ''


#TAG: [G005]
# Verifies: fetch_url returns URL error message on connection failure
@pytest.mark.behavioural
def test_fetch_url_connection_error():
    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("Connection refused")):
        content, status, error = webfetch_mod.fetch_url("http://unreachable.local")
        assert content == ''
        assert status == 0
        assert "URL Error" in error
