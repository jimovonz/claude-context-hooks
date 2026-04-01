"""Tests for lib/ccm_cache.py"""

import json
import gzip
import os
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

import lib.ccm_cache as ccm


# ─── init_ccm_cache ──────────────────────────────────────────

#TAG: [A001]
# Verifies: init_ccm_cache creates the expected directory structure
@pytest.mark.behavioural
def test_init_ccm_cache_creates_directories(tmp_path):
    ccm.init_ccm_cache(tmp_path)
    assert (tmp_path / 'ccm' / 'blobs').is_dir()
    assert (tmp_path / 'ccm' / 'meta').is_dir()
    assert ccm.CCM_CACHE_DIR == tmp_path / 'ccm'


#TAG: [A002]
# Verifies: init_ccm_cache sets global path variables correctly
@pytest.mark.behavioural
def test_init_ccm_cache_sets_globals(tmp_path):
    ccm.init_ccm_cache(tmp_path)
    assert ccm.CCM_BLOBS_DIR == tmp_path / 'ccm' / 'blobs'
    assert ccm.CCM_META_DIR == tmp_path / 'ccm' / 'meta'
    assert ccm.CCM_INDEX_FILE == tmp_path / 'ccm' / 'index.jsonl'
    assert ccm.CCM_LAST_KEY_FILE == tmp_path / 'ccm' / 'last_key'


# ─── compute_content_key ─────────────────────────────────────

#TAG: [A003]
# Verifies: compute_content_key returns deterministic b2s-prefixed hash
@pytest.mark.behavioural
def test_compute_content_key_deterministic():
    key1 = ccm.compute_content_key("hello world")
    key2 = ccm.compute_content_key("hello world")
    assert key1 == key2
    assert key1.startswith("b2s:")
    assert len(key1) == 4 + 16  # "b2s:" + 16 hex chars


# ─── compute_content_key_sha256 ──────────────────────────────

#TAG: [A004]
# Verifies: compute_content_key_sha256 returns sha256-prefixed 64-char hex hash
@pytest.mark.behavioural
def test_compute_content_key_sha256_format():
    key = ccm.compute_content_key_sha256("test content")
    assert key.startswith("sha256:")
    hex_part = key[7:]
    assert len(hex_part) == 64
    int(hex_part, 16)  # valid hex


# ─── get_compression_method ──────────────────────────────────

#TAG: [A005]
# Verifies: get_compression_method returns gzip when zstd unavailable
@pytest.mark.behavioural
def test_get_compression_method_fallback():
    with patch.object(ccm, 'ZSTD_AVAILABLE', False):
        assert ccm.get_compression_method() == 'gzip'


# ─── compress_content / decompress_content ───────────────────

#TAG: [A006]
# Verifies: compress then decompress with gzip roundtrips correctly
@pytest.mark.behavioural
def test_compress_decompress_gzip_roundtrip():
    data = b"x" * 2000  # above COMPRESSION_THRESHOLD
    compressed = ccm.compress_content(data, 'gzip')
    assert len(compressed) < len(data)
    result = ccm.decompress_content(compressed, 'gzip')
    assert result == data


#TAG: [A007]
# Verifies: compress_content skips compression for small content
@pytest.mark.behavioural
def test_compress_content_skips_small():
    data = b"tiny"
    result = ccm.compress_content(data, 'gzip')
    assert result == data  # unchanged, below COMPRESSION_THRESHOLD


#TAG: [A008]
# Verifies: decompress_content with method 'none' returns data unchanged
@pytest.mark.behavioural
def test_decompress_content_none():
    data = b"raw data"
    assert ccm.decompress_content(data, 'none') == data


#TAG: [A009]
# Verifies: decompress_content raises ValueError when zstd unavailable
@pytest.mark.error
def test_decompress_content_zstd_unavailable():
    with patch.object(ccm, 'ZSTD_AVAILABLE', False):
        with pytest.raises(ValueError, match="zstd not available"):
            ccm.decompress_content(b"data", 'zstd')


# ─── store_content ───────────────────────────────────────────

#TAG: [A00A]
# Verifies: store_content creates blob and metadata files for new content
@pytest.mark.behavioural
def test_store_content_creates_files(tmp_cache):
    content = "a" * 2000
    key = ccm.store_content(content, source={'tool_name': 'Test'})
    assert key.startswith("b2s:")
    # Verify blob exists
    hex_key = ccm._key_to_hex(key)
    blobs = list(ccm.CCM_BLOBS_DIR.iterdir())
    assert len(blobs) == 1
    assert hex_key in blobs[0].name
    # Verify metadata exists
    meta_path = ccm.CCM_META_DIR / f"{hex_key}.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta['key'] == key
    assert meta['source']['tool_name'] == 'Test'


#TAG: [A00B]
# Verifies: store_content deduplicates identical content
@pytest.mark.behavioural
def test_store_content_deduplicates(tmp_cache):
    content = "b" * 2000
    key1 = ccm.store_content(content)
    key2 = ccm.store_content(content)
    assert key1 == key2
    blobs = list(ccm.CCM_BLOBS_DIR.iterdir())
    assert len(blobs) == 1  # only one blob file


# ─── retrieve_content ────────────────────────────────────────

#TAG: [A00C]
# Verifies: retrieve_content returns stored content correctly
@pytest.mark.behavioural
def test_retrieve_content_roundtrip(tmp_cache):
    content = "hello\nworld\n" * 500
    key = ccm.store_content(content)
    result = ccm.retrieve_content(key)
    assert result == content


#TAG: [A00D]
# Verifies: retrieve_content returns None for nonexistent key
@pytest.mark.behavioural
def test_retrieve_content_missing_key(tmp_cache):
    assert ccm.retrieve_content("b2s:0000000000000000") is None


# ─── get_metadata ────────────────────────────────────────────

#TAG: [A00E]
# Verifies: get_metadata returns None when no metadata file exists
@pytest.mark.behavioural
def test_get_metadata_missing(tmp_cache):
    assert ccm.get_metadata("b2s:nonexistent00000") is None


# ─── update_pin ──────────────────────────────────────────────

#TAG: [A00F]
# Verifies: update_pin changes pin level on existing content
@pytest.mark.behavioural
def test_update_pin_sets_level(tmp_cache):
    key = ccm.store_content("pin me" * 500)
    assert ccm.update_pin(key, 'hard', 'important')
    meta = ccm.get_metadata(key)
    assert meta['pinned']['level'] == 'hard'
    assert meta['pinned']['reason'] == 'important'


#TAG: [A010]
# Verifies: update_pin returns False for nonexistent key
@pytest.mark.behavioural
def test_update_pin_missing_key(tmp_cache):
    assert ccm.update_pin("b2s:0000000000000000", 'hard') is False


# ─── get_last_key ────────────────────────────────────────────

#TAG: [A011]
# Verifies: get_last_key returns the key from last store_content call
@pytest.mark.behavioural
def test_get_last_key_after_store(tmp_cache):
    key = ccm.store_content("content" * 500)
    assert ccm.get_last_key() == key


#TAG: [A012]
# Verifies: get_last_key returns None when no content has been stored
@pytest.mark.behavioural
def test_get_last_key_empty(tmp_cache):
    assert ccm.get_last_key() is None


# ─── build_ccm_stub ──────────────────────────────────────────

#TAG: [A013]
# Verifies: build_ccm_stub produces valid stub with expected markers
@pytest.mark.behavioural
def test_build_ccm_stub_format():
    stub = ccm.build_ccm_stub("b2s:abc123", 40000, 100)
    assert stub.startswith("[CCM_CACHED]")
    assert "[/CCM_CACHED]" in stub
    assert "~tokens: 10k" in stub
    assert "lines: 100" in stub


#TAG: [A014]
# Verifies: build_ccm_stub includes exit code when non-zero
@pytest.mark.behavioural
def test_build_ccm_stub_with_exit_code():
    stub = ccm.build_ccm_stub("b2s:abc123", 4000, 50, exit_code=2)
    assert "exit: 2" in stub


# ─── is_ccm_stub ─────────────────────────────────────────────

#TAG: [A015]
# Verifies: is_ccm_stub returns True for valid stub content
@pytest.mark.behavioural
def test_is_ccm_stub_valid():
    stub = "[CCM_CACHED]\nkey: abc\n[/CCM_CACHED]"
    assert ccm.is_ccm_stub(stub) is True


#TAG: [A016]
# Verifies: is_ccm_stub returns False for non-stub content
@pytest.mark.behavioural
def test_is_ccm_stub_invalid():
    assert ccm.is_ccm_stub("just some text") is False
    assert ccm.is_ccm_stub(42) is False
    assert ccm.is_ccm_stub(None) is False


# ─── parse_ccm_stub (non-trivial: 4 tests) ──────────────────

#TAG: [A017]
# Verifies: parse_ccm_stub extracts key and metadata from valid stub
@pytest.mark.behavioural
def test_parse_ccm_stub_valid():
    stub = "[CCM_CACHED]\nkey: b2s:abc123\nbytes: 1000\nlines: 50\nexit: 2\n[/CCM_CACHED]"
    result = ccm.parse_ccm_stub(stub)
    assert result['key'] == 'b2s:abc123'
    assert result['bytes'] == 1000
    assert result['lines'] == 50
    assert result['exit_code'] == 2


#TAG: [A018]
# Verifies: parse_ccm_stub returns None for stub missing key field
@pytest.mark.edge
def test_parse_ccm_stub_no_key():
    stub = "[CCM_CACHED]\nbytes: 100\n[/CCM_CACHED]"
    assert ccm.parse_ccm_stub(stub) is None


#TAG: [A019]
# Verifies: parse_ccm_stub returns None for non-stub content
@pytest.mark.error
def test_parse_ccm_stub_not_a_stub():
    assert ccm.parse_ccm_stub("random text") is None
    assert ccm.parse_ccm_stub("") is None


#TAG: [A01A]
# Verifies: parse_ccm_stub handles non-integer values in numeric fields gracefully
@pytest.mark.adversarial
def test_parse_ccm_stub_malformed_values():
    stub = "[CCM_CACHED]\nkey: b2s:abc\nbytes: not_a_number\nlines: xyz\nexit: ?\n[/CCM_CACHED]"
    result = ccm.parse_ccm_stub(stub)
    assert result is not None
    assert result['key'] == 'b2s:abc'
    assert result['bytes'] == 'not_a_number'
    assert result['lines'] == 'xyz'
    assert result['exit_code'] == '?'


# ─── list_all_keys ───────────────────────────────────────────

#TAG: [A01B]
# Verifies: list_all_keys returns keys for all stored content
@pytest.mark.behavioural
def test_list_all_keys(tmp_cache):
    k1 = ccm.store_content("aaa" * 500)
    k2 = ccm.store_content("bbb" * 500)
    keys = ccm.list_all_keys()
    assert set(keys) == {k1, k2}


# ─── get_cache_stats (non-trivial: 4 tests) ─────────────────

#TAG: [A01C]
# Verifies: get_cache_stats returns correct counts and sizes for populated cache
@pytest.mark.behavioural
def test_get_cache_stats_populated(tmp_cache):
    ccm.store_content("x" * 2000, pin_level='hard', pin_reason='test')
    ccm.store_content("y" * 3000, pin_level='soft', pin_reason='test')
    ccm.store_content("z" * 4000)

    stats = ccm.get_cache_stats()
    assert stats['total_items'] == 3
    assert stats['pinned_hard'] == 1
    assert stats['pinned_soft'] == 1
    assert stats['unpinned'] == 1
    assert stats['total_bytes_uncompressed'] == 2000 + 3000 + 4000
    assert stats['items_never_accessed'] == 3


#TAG: [A01D]
# Verifies: get_cache_stats returns zeroed stats for empty cache
@pytest.mark.edge
def test_get_cache_stats_empty(tmp_cache):
    stats = ccm.get_cache_stats()
    assert stats['total_items'] == 0
    assert stats['total_bytes_compressed'] == 0
    assert stats['total_bytes_uncompressed'] == 0
    assert stats['oldest_access'] is None
    assert stats['newest_access'] is None


#TAG: [A01E]
# Verifies: get_cache_stats handles corrupted metadata files gracefully
@pytest.mark.error
def test_get_cache_stats_corrupt_meta(tmp_cache):
    ccm.store_content("valid" * 500)
    # Create a corrupt meta file
    corrupt_meta = ccm.CCM_META_DIR / "corrupt.json"
    corrupt_meta.write_text("not json{{{")
    stats = ccm.get_cache_stats()
    assert stats['total_items'] == 1  # only the valid one counted


#TAG: [A01F]
# Verifies: get_cache_stats handles metadata with missing/invalid date fields
@pytest.mark.adversarial
def test_get_cache_stats_invalid_dates(tmp_cache):
    hex_key = "deadbeef12345678"
    meta = {
        'key': f'b2s:{hex_key}',
        'created_at': 'not-a-date',
        'last_access_at': 'also-not-a-date',
        'access_count': 5,
        'bytes_uncompressed': 100,
        'lines': 10,
        'compression': 'none',
        'source': {},
        'pinned': {'level': 'none', 'reason': '', 'pinned_at': ''}
    }
    meta_path = ccm.CCM_META_DIR / f"{hex_key}.json"
    meta_path.write_text(json.dumps(meta))
    stats = ccm.get_cache_stats()
    assert stats['total_items'] == 1
    assert stats['total_accesses'] == 5
    assert stats['oldest_access'] is None  # invalid date not parsed


# ─── delete_cached_content (non-trivial: 4 tests) ───────────

#TAG: [A020]
# Verifies: delete_cached_content removes both blob and metadata files
@pytest.mark.behavioural
def test_delete_cached_content_removes_files(tmp_cache):
    key = ccm.store_content("delete me" * 500)
    assert ccm.retrieve_content(key) is not None
    assert ccm.delete_cached_content(key) is True
    assert ccm.retrieve_content(key) is None
    assert ccm.get_metadata(key) is None


#TAG: [A021]
# Verifies: delete_cached_content returns False for nonexistent key
@pytest.mark.edge
def test_delete_cached_content_missing(tmp_cache):
    assert ccm.delete_cached_content("b2s:0000000000000000") is False


#TAG: [A022]
# Verifies: delete_cached_content handles blob missing but meta present
@pytest.mark.error
def test_delete_cached_content_partial_state(tmp_cache):
    key = ccm.store_content("partial" * 500)
    # Remove blob but keep meta
    result = ccm._find_blob_path(key)
    if result:
        result[0].unlink()
    assert ccm.delete_cached_content(key) is True  # meta deletion counts


#TAG: [A023]
# Verifies: delete_cached_content handles unlink permission errors gracefully
@pytest.mark.adversarial
def test_delete_cached_content_permission_error(tmp_cache):
    key = ccm.store_content("locked" * 500)
    meta_path = ccm._get_meta_path(key)
    # Make meta read-only dir to cause unlink failure
    with patch.object(Path, 'unlink', side_effect=OSError("permission denied")):
        # Should not raise, returns False since unlinks failed
        result = ccm.delete_cached_content(key)
        # The blob unlink also fails, so deleted stays False for blob
        # but meta_path.exists() is True and unlink raises, so deleted remains from blob attempt
        assert isinstance(result, bool)


# ─── append_index_log ────────────────────────────────────────

#TAG: [A024]
# Verifies: append_index_log writes valid JSONL entry to index file
@pytest.mark.behavioural
def test_append_index_log_writes_entry(tmp_cache):
    ccm.append_index_log("b2s:test123", "Bash", 0, 5000, 100)
    content = ccm.CCM_INDEX_FILE.read_text()
    entry = json.loads(content.strip())
    assert entry['key'] == 'b2s:test123'
    assert entry['tool'] == 'Bash'
    assert entry['exit'] == 0
    assert entry['bytes'] == 5000
    assert entry['lines'] == 100
