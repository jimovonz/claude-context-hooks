import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / 'hooks'

sys.path.insert(0, str(HOOKS_DIR))


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect ccm cache to a tmp dir for the duration of a test."""
    cache_root = tmp_path / 'cache'
    cache_root.mkdir()
    from lib import ccm_cache
    ccm_cache.init_ccm_cache(cache_root)
    yield cache_root


@pytest.fixture
def run_hook(tmp_path, monkeypatch):
    """Run a hook script as a subprocess with a JSON stdin payload.

    Returns a callable: run_hook(hook_path, payload_dict, env=None) ->
    (returncode, stdout_obj_or_str, stderr_str).
    Stdout is parsed as JSON when possible.
    """
    def _run(hook_path: Path, payload: dict, env=None, decode_json=True):
        environ = os.environ.copy()
        environ['HOME'] = str(tmp_path)
        if env:
            environ.update(env)
        proc = subprocess.run(
            [sys.executable, str(hook_path)],
            input=json.dumps(payload).encode('utf-8'),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environ,
            timeout=10,
        )
        out = proc.stdout.decode('utf-8', errors='replace').strip()
        err = proc.stderr.decode('utf-8', errors='replace')
        parsed = out
        if decode_json and out:
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError:
                pass
        return proc.returncode, parsed, err

    return _run
