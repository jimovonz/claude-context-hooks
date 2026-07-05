"""Tests for glob-scoped rules (lib/cch_rules.py + cache-wrap footer)."""
import os
import subprocess
import sys
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
CACHE_WRAP = HOOKS / 'cache-wrap.py'
sys.path.insert(0, str(HOOKS))
from lib.cch_rules import rules_footer, _parse_rule  # noqa: E402

RULE = """---
globs: prisma/**, *.prisma
---
Schema changes require the manual prod migration dance.
"""


def make_project(tmp_path):
    (tmp_path / '.cch' / 'rules').mkdir(parents=True)
    (tmp_path / '.cch' / 'rules' / 'prisma.md').write_text(RULE)
    (tmp_path / 'prisma').mkdir()
    (tmp_path / 'prisma' / 'schema.prisma').write_text('model X { id Int @id }\n')
    (tmp_path / 'readme.txt').write_text('hello\n')
    return tmp_path


def fresh_env(tmp_path):
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)          # marker dir lands in tmp HOME
    env['CCH_SESSION_ID'] = uuid.uuid4().hex
    return env


def test_parse_rule(tmp_path):
    f = tmp_path / 'r.md'; f.write_text(RULE)
    globs, body = _parse_rule(f)
    assert globs == ['prisma/**', '*.prisma']
    assert 'migration dance' in body


def test_footer_fires_once_per_session(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CCH_SESSION_ID', uuid.uuid4().hex)
    import lib.cch_rules as cr
    monkeypatch.setattr(cr, 'SEEN_DIR', tmp_path / 'seen')
    cmd = f'cat {proj}/prisma/schema.prisma'
    first = rules_footer(cmd, str(proj))
    assert first and 'migration dance' in first and 'cch-rule: prisma.md' in first
    assert rules_footer(cmd, str(proj)) is None  # deduped


def test_non_matching_file_no_footer(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    import lib.cch_rules as cr
    monkeypatch.setattr(cr, 'SEEN_DIR', tmp_path / 'seen2')
    assert rules_footer(f'cat {proj}/readme.txt', str(proj)) is None


def test_rules_dir_found_from_subdir(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    import lib.cch_rules as cr
    monkeypatch.setattr(cr, 'SEEN_DIR', tmp_path / 'seen3')
    monkeypatch.setenv('CCH_SESSION_ID', uuid.uuid4().hex)
    out = rules_footer(f'cat {proj}/prisma/schema.prisma', str(proj / 'prisma'))
    assert out and 'migration dance' in out


def test_end_to_end_through_cache_wrap(tmp_path):
    proj = make_project(tmp_path)
    env = fresh_env(tmp_path)
    p = subprocess.run([sys.executable, str(CACHE_WRAP), '--', f'cat {proj}/prisma/schema.prisma'],
                       stdout=subprocess.PIPE, env=env, cwd=str(proj), timeout=60)
    out = p.stdout.decode()
    assert 'model X' in out and 'migration dance' in out
    # same session, second read: rule must NOT repeat
    p2 = subprocess.run([sys.executable, str(CACHE_WRAP), '--', f'cat {proj}/prisma/schema.prisma'],
                        stdout=subprocess.PIPE, env=env, cwd=str(proj), timeout=60)
    assert 'migration dance' not in p2.stdout.decode()
