"""Atomic file writes that never clobber symlinks.

`os.replace()` over a symlink replaces the LINK with a regular file and leaves
the real file untouched — a silent lost update. This project installs its own
hooks as symlinks into ~/.claude/hooks/, so editing an installed path used to
orphan the edit while printing a successful diff.

Every writer here:
  1. resolves symlinks first (so the real file is the one replaced),
  2. stages through a UNIQUE temp file in the destination directory, so two
     concurrent writers can never share a staging path (the old fixed
     `<name>.cch-tmp` raced across tool calls; the cch-batch same-file guard
     only ever covered writers inside a single batch),
  3. preserves mode, fsyncs, then renames.
"""
import os
import tempfile
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]


def resolve_target(path: PathLike) -> Path:
    """Real path of `path`, with symlinks followed."""
    return Path(os.path.realpath(str(path)))


def _default_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def atomic_write_bytes(target: PathLike, data: bytes,
                       mode: Optional[int] = None) -> Path:
    """Write `data` to `target` atomically. Returns the resolved real path."""
    real = resolve_target(target)
    real.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = os.stat(real).st_mode & 0o7777
        except OSError:
            mode = _default_mode()

    fd, tmp_name = tempfile.mkstemp(
        dir=str(real.parent), prefix=f'.{real.name}.', suffix='.cch-tmp')
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return real


def atomic_write_text(target: PathLike, text: str,
                      encoding: str = 'utf-8',
                      mode: Optional[int] = None) -> Path:
    return atomic_write_bytes(target, text.encode(encoding), mode=mode)
