#!/usr/bin/env python3
"""
Content-addressable durable cache for Claude Context Manager.
Provides SHA256-based deduplication, compression, and pinning.

Storage layout:
    ~/.claude/cache/ccm/
        blobs/<sha256>.zst    # Compressed content
        meta/<sha256>.json    # Metadata sidecar
        index.jsonl           # Append-only log of cache writes
        last_key              # Most recent cache key
"""

import gzip
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal, TypedDict

# Try to import zstandard for better compression
try:
    import zstandard as zstd
    ZSTD_AVAILABLE = True
except ImportError:
    ZSTD_AVAILABLE = False


# Type definitions
class SourceInfo(TypedDict, total=False):
    session_path: str
    tool_name: str
    exit_code: int
    command: str
    cwd: str


class PinInfo(TypedDict, total=False):
    level: Literal['none', 'soft', 'hard']
    reason: str
    pinned_at: str


class CacheMeta(TypedDict):
    key: str  # "sha256:<hex>"
    created_at: str  # ISO format
    last_access_at: str  # ISO format
    access_count: int  # Number of times content was retrieved
    bytes_uncompressed: int
    lines: int
    compression: Literal['zstd', 'gzip', 'none']
    source: SourceInfo
    pinned: PinInfo


# Cache paths - will be set by init_ccm_cache()
CCM_CACHE_DIR: Optional[Path] = None
CCM_BLOBS_DIR: Optional[Path] = None
CCM_META_DIR: Optional[Path] = None
CCM_INDEX_FILE: Optional[Path] = None
CCM_LAST_KEY_FILE: Optional[Path] = None

# Compression threshold - don't compress small content
COMPRESSION_THRESHOLD = 1024  # 1KB


def init_ccm_cache(base_dir: Optional[Path] = None) -> None:
    """
    Initialize CCM cache directory structure.
    Creates ~/.claude/cache/ccm/{blobs,meta}/ if not exists.
    """
    global CCM_CACHE_DIR, CCM_BLOBS_DIR, CCM_META_DIR, CCM_INDEX_FILE, CCM_LAST_KEY_FILE

    if base_dir is None:
        base_dir = Path.home() / '.claude' / 'cache'

    CCM_CACHE_DIR = base_dir / 'ccm'
    CCM_BLOBS_DIR = CCM_CACHE_DIR / 'blobs'
    CCM_META_DIR = CCM_CACHE_DIR / 'meta'
    CCM_INDEX_FILE = CCM_CACHE_DIR / 'index.jsonl'
    CCM_LAST_KEY_FILE = CCM_CACHE_DIR / 'last_key'

    # Create directories with secure permissions
    for d in [CCM_CACHE_DIR, CCM_BLOBS_DIR, CCM_META_DIR]:
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def _ensure_initialized() -> None:
    """Ensure cache is initialized."""
    if CCM_CACHE_DIR is None:
        init_ccm_cache()


def compute_content_key(content: str) -> str:
    """
    Compute BLAKE2s hash of content (64-bit, 16 hex chars).
    Returns: "b2s:XXXXXXXXXXXXXXXX"

    BLAKE2s with 8-byte digest provides:
    - 64 bits = 16 hex characters (vs SHA-256's 64)
    - Negligible collision risk for ~100k items
    - Built into Python 3.6+
    """
    content_bytes = content.encode('utf-8')
    hash_hex = hashlib.blake2s(content_bytes, digest_size=8).hexdigest()
    return f"b2s:{hash_hex}"


def compute_content_key_sha256(content: str) -> str:
    """
    Legacy SHA256 hash computation.
    Returns: "sha256:<64-char-hex>"
    """
    content_bytes = content.encode('utf-8')
    hash_hex = hashlib.sha256(content_bytes).hexdigest()
    return f"sha256:{hash_hex}"


def _key_to_hex(key: str) -> str:
    """Extract hex portion from key (supports both b2s: and sha256: prefixes)."""
    if key.startswith('b2s:'):
        return key[4:]
    if key.startswith('sha256:'):
        return key[7:]
    return key


def _get_key_prefix(key: str) -> str:
    """Get the prefix/type of a key."""
    if key.startswith('b2s:'):
        return 'b2s'
    if key.startswith('sha256:'):
        return 'sha256'
    return 'unknown'


def get_compression_method() -> Literal['zstd', 'gzip', 'none']:
    """
    Determine best available compression method.
    Preference: zstd > gzip > none
    """
    if ZSTD_AVAILABLE:
        return 'zstd'
    return 'gzip'


def compress_content(content: bytes, method: str) -> bytes:
    """
    Compress content using specified method.
    Falls back gracefully if method unavailable.
    """
    if len(content) < COMPRESSION_THRESHOLD:
        return content  # Don't compress small content

    if method == 'zstd' and ZSTD_AVAILABLE:
        cctx = zstd.ZstdCompressor(level=3)
        return cctx.compress(content)
    elif method == 'gzip' or (method == 'zstd' and not ZSTD_AVAILABLE):
        return gzip.compress(content, compresslevel=6)
    else:
        return content


def decompress_content(data: bytes, method: str) -> bytes:
    """
    Decompress content using specified method.
    """
    if method == 'zstd':
        if not ZSTD_AVAILABLE:
            raise ValueError("zstd not available for decompression")
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)
    elif method == 'gzip':
        return gzip.decompress(data)
    else:
        return data


def _get_blob_path(key: str, method: str) -> Path:
    """Get blob file path for key and compression method."""
    _ensure_initialized()
    hex_key = _key_to_hex(key)
    ext = {'zstd': '.zst', 'gzip': '.gz', 'none': '.txt'}[method]
    return CCM_BLOBS_DIR / f"{hex_key}{ext}"


def _get_meta_path(key: str) -> Path:
    """Get metadata file path for key."""
    _ensure_initialized()
    hex_key = _key_to_hex(key)
    return CCM_META_DIR / f"{hex_key}.json"


def _find_blob_path(key: str) -> Optional[tuple[Path, str]]:
    """Find existing blob path for key. Returns (path, compression_method) or None."""
    _ensure_initialized()
    hex_key = _key_to_hex(key)

    for ext, method in [('.zst', 'zstd'), ('.gz', 'gzip'), ('.txt', 'none')]:
        path = CCM_BLOBS_DIR / f"{hex_key}{ext}"
        if path.exists():
            return path, method
    return None


def store_content(
    content: str,
    source: Optional[SourceInfo] = None,
    pin_level: Literal['none', 'soft', 'hard'] = 'none',
    pin_reason: str = ''
) -> str:
    """
    Store content in durable cache with deduplication.

    Args:
        content: The content to cache
        source: Optional source metadata (session, tool, etc.)
        pin_level: Initial pin level
        pin_reason: Reason for pinning

    Returns:
        Cache key ("sha256:<hex>")

    Side effects:
        - Creates blob file if content is new
        - Creates/updates meta file
        - Updates last_key file
        - Appends to index log
    """
    _ensure_initialized()

    key = compute_content_key(content)
    hex_key = _key_to_hex(key)
    content_bytes = content.encode('utf-8')
    lines = content.count('\n')
    now = datetime.now().isoformat()

    # Check if blob already exists (deduplication)
    existing = _find_blob_path(key)
    if existing:
        blob_path, compression = existing
        # Update metadata (access time, possibly pin)
        meta = get_metadata(key)
        if meta:
            meta['last_access_at'] = now
            if pin_level != 'none' and meta['pinned'].get('level', 'none') == 'none':
                meta['pinned'] = {
                    'level': pin_level,
                    'reason': pin_reason,
                    'pinned_at': now
                }
            _save_metadata(key, meta)
    else:
        # New content - compress and store
        compression = get_compression_method()
        compressed = compress_content(content_bytes, compression)

        # Determine actual compression used
        if len(compressed) >= len(content_bytes):
            # Compression didn't help, store uncompressed
            compression = 'none'
            compressed = content_bytes

        blob_path = _get_blob_path(key, compression)
        blob_path.write_bytes(compressed)
        try:
            os.chmod(blob_path, 0o600)
        except OSError:
            pass

        # Create metadata
        meta: CacheMeta = {
            'key': key,
            'created_at': now,
            'last_access_at': now,
            'access_count': 0,
            'bytes_uncompressed': len(content_bytes),
            'lines': lines,
            'compression': compression,
            'source': source or {},
            'pinned': {
                'level': pin_level,
                'reason': pin_reason if pin_level != 'none' else '',
                'pinned_at': now if pin_level != 'none' else ''
            }
        }
        _save_metadata(key, meta)

        # Append to index log
        append_index_log(
            key,
            source.get('tool_name', 'unknown') if source else 'unknown',
            source.get('exit_code', 0) if source else 0,
            len(content_bytes),
            lines
        )

    # Update last_key
    CCM_LAST_KEY_FILE.write_text(key + '\n')

    return key


def _save_metadata(key: str, meta: CacheMeta) -> None:
    """Save metadata to sidecar file."""
    meta_path = _get_meta_path(key)
    meta_path.write_text(json.dumps(meta, indent=2))
    try:
        os.chmod(meta_path, 0o600)
    except OSError:
        pass


def retrieve_content(key: str) -> Optional[str]:
    """
    Retrieve content from cache by key.
    Updates last_access_at in metadata.
    Returns None if not found.
    """
    _ensure_initialized()

    result = _find_blob_path(key)
    if not result:
        return None

    blob_path, compression = result

    try:
        compressed_data = blob_path.read_bytes()
        content_bytes = decompress_content(compressed_data, compression)
        content = content_bytes.decode('utf-8')

        # Integrity guard: b2s keys ARE blake2s(content), so we can recompute
        # and detect on-disk/decompression corruption rather than handing back
        # silently-wrong bytes. Only b2s keys are content-addressed this way.
        if key.startswith('b2s:'):
            recomputed = compute_content_key(content)
            if recomputed != key:
                import sys as _sys
                _sys.stderr.write(
                    f"ccm-get: WARNING integrity check failed for {key} "
                    f"(recomputed {recomputed}) — blob may be corrupted\n"
                )

        # Update access time and count
        meta = get_metadata(key)
        if meta:
            meta['last_access_at'] = datetime.now().isoformat()
            meta['access_count'] = meta.get('access_count', 0) + 1
            _save_metadata(key, meta)

        return content
    except Exception:
        return None


def get_metadata(key: str) -> Optional[CacheMeta]:
    """
    Get metadata for cached content.
    Returns None if not found.
    """
    _ensure_initialized()

    meta_path = _get_meta_path(key)
    if not meta_path.exists():
        return None

    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_pin(
    key: str,
    level: Literal['none', 'soft', 'hard'],
    reason: str = ''
) -> bool:
    """
    Update pin level for cached content.
    Returns True if successful, False if key not found.
    """
    _ensure_initialized()

    meta = get_metadata(key)
    if not meta:
        return False

    now = datetime.now().isoformat()
    meta['pinned'] = {
        'level': level,
        'reason': reason if level != 'none' else '',
        'pinned_at': now if level != 'none' else ''
    }
    _save_metadata(key, meta)
    return True


def get_last_key() -> Optional[str]:
    """
    Get the most recently cached key.
    Used for "pin last" resolution.
    """
    _ensure_initialized()

    if not CCM_LAST_KEY_FILE.exists():
        return None

    try:
        return CCM_LAST_KEY_FILE.read_text().strip()
    except OSError:
        return None


def build_ccm_stub(
    key: str,
    bytes_uncompressed: int,
    lines: int,
    exit_code: int = 0,
    pin_level: str = 'none',
    tool_name: str = '',
    file_path: str = '',
    command: str = '',
    description: str = ''
) -> str:
    """
    Build CCM stub format string (minimal fields).

    Returns:
        [CCM_CACHED]
        key: sha256:<hex>
        ~tokens: <estimated>
        lines: <lines>
        [/CCM_CACHED]
    """
    hex_key = _key_to_hex(key)

    # Build source line from available metadata
    source_parts = []
    if tool_name:
        source_parts.append(tool_name)
    if file_path:
        # Shorten home paths
        if file_path.startswith(str(Path.home())):
            file_path = '~' + file_path[len(str(Path.home())):]
        source_parts.append(file_path)
    elif command:
        # Truncate long commands
        cmd_display = command[:80] + '...' if len(command) > 80 else command
        source_parts.append(cmd_display)

    source_line = ' '.join(source_parts) if source_parts else 'unknown'

    # Build stub - minimal fields only (key shown in Retrieve line)
    stub_lines = ['[CCM_CACHED]']
    # Estimate tokens (~4 chars per token for typical text)
    est_tokens = bytes_uncompressed // 4
    if est_tokens >= 1000:
        stub_lines.append(f'~tokens: {est_tokens // 1000}k')
    else:
        stub_lines.append(f'~tokens: {est_tokens}')
    stub_lines.append(f'lines: {lines}')
    if exit_code != 0:
        stub_lines.append(f'exit: {exit_code}')
    # Self-check digit over the stub's own identifying fields. Lets a
    # garbled-in-transit stub be detected (paste back via ccm-get --check)
    # instead of silently extracting a wrong key and chasing a phantom.
    stub_lines.append(f'check: {_stub_check_digit(hex_key, lines, exit_code)}')
    stub_lines.append('[/CCM_CACHED]')

    return '\n'.join(stub_lines)


def _stub_check_digit(hex_key: str, lines: int, exit_code: int) -> str:
    """Short checksum over a stub's identifying fields (key+lines+exit).

    4 hex chars from blake2s. Not cryptographic — purely a transit-integrity
    guard so a corrupted stub key is self-detecting rather than silently
    resolving to 'not found'.
    """
    canonical = f"{hex_key}:{lines}:{exit_code}".encode('utf-8')
    return hashlib.blake2s(canonical, digest_size=2).hexdigest()


def _extract_key_token(text: str) -> Optional[str]:
    """Pull the first b2s:/sha256: key token from arbitrary text.

    The cached key lives in the 'Retrieve: ccm-get.py <key> ...' line that
    cache-wrap emits next to the stub, not inside the stub body — so stub
    verification must look at the whole emitted block.
    """
    import re
    m = re.search(r'\b(b2s:[0-9a-f]+|sha256:[0-9a-f]+)\b', text)
    return m.group(1) if m else None


def verify_ccm_stub(content: str) -> Optional[bool]:
    """Verify a cached block's check digit against its key + fields.

    `content` is the full emitted block (stub + Retrieve line) as pasted
    back. Returns True if the check digit matches, False if present but
    wrong (garbled in transit), None if no check digit is present (legacy
    block or not a stub) — callers treat None as 'cannot verify'.
    """
    if not is_ccm_stub(content):
        return None
    # Pull check digit directly from the stub body (parse_ccm_stub requires
    # a 'key' field these stubs lack, so scan lines here instead).
    check = None
    lines_val = 0
    exit_code = 0
    for line in content.split('\n'):
        s = line.strip()
        if s.startswith('check:'):
            check = s.partition(':')[2].strip()
        elif s.startswith('lines:'):
            try:
                lines_val = int(s.partition(':')[2].strip())
            except ValueError:
                pass
        elif s.startswith('exit:'):
            try:
                exit_code = int(s.partition(':')[2].strip())
            except ValueError:
                pass
    if check is None:
        return None
    key = _extract_key_token(content)
    hex_key = _key_to_hex(key) if key else ''
    expected = _stub_check_digit(hex_key, lines_val, exit_code)
    return check == expected


def parse_ccm_stub(content: str) -> Optional[dict]:
    """
    Parse CCM stub to extract key and metadata.
    Returns None if not a valid stub.
    """
    if not is_ccm_stub(content):
        return None

    result = {}
    for line in content.split('\n'):
        line = line.strip()
        if ':' in line and not line.startswith('['):
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip()
            if key == 'bytes' or key == 'lines':
                try:
                    result[key] = int(value)
                except ValueError:
                    result[key] = value
            elif key == 'exit':
                try:
                    result['exit_code'] = int(value)
                except ValueError:
                    result['exit_code'] = value
            else:
                result[key] = value

    return result if 'key' in result else None


def is_ccm_stub(content: str) -> bool:
    """
    Check if content is a CCM stub.
    """
    if not isinstance(content, str):
        return False
    return content.strip().startswith('[CCM_CACHED]') and '[/CCM_CACHED]' in content


def list_all_keys() -> list[str]:
    """
    List all cached content keys.
    """
    _ensure_initialized()

    keys = []
    for meta_file in CCM_META_DIR.glob('*.json'):
        try:
            meta = json.loads(meta_file.read_text())
            if 'key' in meta:
                keys.append(meta['key'])
        except (json.JSONDecodeError, OSError):
            continue
    return keys


def get_cache_stats() -> dict:
    """
    Get cache statistics.
    Returns: {
        'total_items': int,
        'total_bytes_compressed': int,
        'total_bytes_uncompressed': int,
        'pinned_hard': int,
        'pinned_soft': int,
        'unpinned': int,
        'oldest_access': datetime or None,
        'newest_access': datetime or None,
        'total_accesses': int,
        'items_accessed': int,
        'items_never_accessed': int,
        'max_access_count': int,
    }
    """
    _ensure_initialized()

    stats = {
        'total_items': 0,
        'total_bytes_compressed': 0,
        'total_bytes_uncompressed': 0,
        'pinned_hard': 0,
        'pinned_soft': 0,
        'unpinned': 0,
        'oldest_access': None,
        'newest_access': None,
        'total_accesses': 0,
        'items_accessed': 0,
        'items_never_accessed': 0,
        'max_access_count': 0,
    }

    for meta_file in CCM_META_DIR.glob('*.json'):
        try:
            meta = json.loads(meta_file.read_text())
            stats['total_items'] += 1
            stats['total_bytes_uncompressed'] += meta.get('bytes_uncompressed', 0)

            # Count by pin level
            pin_level = meta.get('pinned', {}).get('level', 'none')
            if pin_level == 'hard':
                stats['pinned_hard'] += 1
            elif pin_level == 'soft':
                stats['pinned_soft'] += 1
            else:
                stats['unpinned'] += 1

            # Track access times
            access_str = meta.get('last_access_at')
            if access_str:
                try:
                    access_time = datetime.fromisoformat(access_str)
                    if stats['oldest_access'] is None or access_time < stats['oldest_access']:
                        stats['oldest_access'] = access_time
                    if stats['newest_access'] is None or access_time > stats['newest_access']:
                        stats['newest_access'] = access_time
                except ValueError:
                    pass

            # Track access counts
            access_count = meta.get('access_count', 0)
            stats['total_accesses'] += access_count
            if access_count > 0:
                stats['items_accessed'] += 1
            else:
                stats['items_never_accessed'] += 1
            if access_count > stats['max_access_count']:
                stats['max_access_count'] = access_count

        except (json.JSONDecodeError, OSError):
            continue

    # Calculate compressed size from actual blob files
    for blob_file in CCM_BLOBS_DIR.iterdir():
        if blob_file.is_file():
            stats['total_bytes_compressed'] += blob_file.stat().st_size

    return stats


def append_index_log(
    key: str,
    tool_name: str,
    exit_code: int,
    bytes_size: int,
    lines: int
) -> None:
    """
    Append entry to index log for auditing.
    """
    _ensure_initialized()

    entry = {
        'ts': datetime.now().isoformat(),
        'key': key,
        'tool': tool_name,
        'exit': exit_code,
        'bytes': bytes_size,
        'lines': lines
    }

    try:
        with open(CCM_INDEX_FILE, 'a') as f:
            f.write(json.dumps(entry, separators=(',', ':')) + '\n')
    except OSError:
        pass


def delete_cached_content(key: str) -> bool:
    """
    Delete cached content and metadata.
    Returns True if deleted, False if not found.
    """
    _ensure_initialized()

    deleted = False

    # Delete blob
    result = _find_blob_path(key)
    if result:
        try:
            result[0].unlink()
            deleted = True
        except OSError:
            pass

    # Delete metadata
    meta_path = _get_meta_path(key)
    if meta_path.exists():
        try:
            meta_path.unlink()
            deleted = True
        except OSError:
            pass

    return deleted
