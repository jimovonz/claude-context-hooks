"""Tests for stub transit-integrity check digit and retrieval integrity guard.

Covers the guard added after a session where garbled large-output reads led
to fabricated cache keys and a phantom 'cache expiry' theory. The check digit
makes a corrupted stub self-detecting; the retrieval guard makes a corrupted
blob self-detecting (b2s keys are blake2s(content)).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'hooks'))

from lib.ccm_cache import (
    init_ccm_cache, store_content, build_ccm_stub, verify_ccm_stub,
    _stub_check_digit, _find_blob_path, retrieve_content,
)


def _block(key, lines=400, exit_code=0):
    """Reproduce cache-wrap's full emitted block: stub + Retrieve line."""
    stub = build_ccm_stub(key=key, bytes_uncompressed=lines * 20,
                          lines=lines, exit_code=exit_code)
    return stub + f'\nRetrieve: ccm-get.py {key} [--grep PATTERN]\n'


def test_stub_contains_check_digit(tmp_path):
    init_ccm_cache(tmp_path / 'cache')
    stub = build_ccm_stub(key='b2s:0123456789abcdef',
                          bytes_uncompressed=8000, lines=400)
    assert 'check:' in stub


def test_intact_block_verifies_true(tmp_path):
    init_ccm_cache(tmp_path / 'cache')
    block = _block('b2s:0123456789abcdef')
    assert verify_ccm_stub(block) is True


def test_tampered_key_verifies_false(tmp_path):
    init_ccm_cache(tmp_path / 'cache')
    key = 'b2s:0123456789abcdef'
    block = _block(key)
    # flip first hex char of the key everywhere it appears
    tampered = block.replace(key, 'b2s:f123456789abcdef')
    assert verify_ccm_stub(tampered) is False


def test_tampered_lines_verifies_false(tmp_path):
    init_ccm_cache(tmp_path / 'cache')
    block = _block('b2s:0123456789abcdef', lines=400)
    tampered = block.replace('lines: 400', 'lines: 399')
    assert verify_ccm_stub(tampered) is False


def test_legacy_block_without_check_returns_none(tmp_path):
    init_ccm_cache(tmp_path / 'cache')
    legacy = ('[CCM_CACHED]\n~tokens: 2k\nlines: 400\n[/CCM_CACHED]\n'
              'Retrieve: ccm-get.py b2s:0123456789abcdef\n')
    assert verify_ccm_stub(legacy) is None


def test_non_stub_returns_none(tmp_path):
    init_ccm_cache(tmp_path / 'cache')
    assert verify_ccm_stub('just some random text\n') is None


def test_check_digit_stable_for_same_fields():
    a = _stub_check_digit('0123456789abcdef', 400, 0)
    b = _stub_check_digit('0123456789abcdef', 400, 0)
    assert a == b
    assert a != _stub_check_digit('0123456789abcdef', 401, 0)
    assert a != _stub_check_digit('f123456789abcdef', 400, 0)


def test_round_trip_real_store(tmp_path):
    init_ccm_cache(tmp_path / 'cache')
    content = '\n'.join(f'ROW-{i:04d}' for i in range(400)) + '\n'
    key = store_content(content, source={'tool_name': 'Bash'})
    block = _block(key, lines=content.count('\n'))
    assert verify_ccm_stub(block) is True


def test_retrieval_integrity_intact(tmp_path, capsys):
    init_ccm_cache(tmp_path / 'cache')
    content = 'good content line\n' * 50
    key = store_content(content, source={'tool_name': 'Bash'})
    got = retrieve_content(key)
    assert got == content
    # no integrity warning on a clean blob
    assert 'integrity check failed' not in capsys.readouterr().err


def test_retrieval_integrity_detects_corruption(tmp_path, capsys):
    import gzip
    init_ccm_cache(tmp_path / 'cache')
    content = 'good content line\n' * 50
    key = store_content(content, source={'tool_name': 'Bash'})
    blob_path, method = _find_blob_path(key)
    # overwrite blob with different content under the same key's filename
    corrupt = b'TOTALLY DIFFERENT CONTENT\n'
    blob_path.write_bytes(gzip.compress(corrupt) if method == 'gzip' else corrupt)
    retrieve_content(key)
    assert 'integrity check failed' in capsys.readouterr().err
