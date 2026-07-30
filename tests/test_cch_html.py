"""Tests for cch-html.py — converter, selectors, shell detection, escalation."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HTML_TOOL = Path(__file__).resolve().parent.parent / 'hooks' / 'cch-html.py'
CACHE_WRAP = Path(__file__).resolve().parent.parent / 'hooks' / 'cache-wrap.py'

PAGE = """<!doctype html><html><head><title>T</title><style>.x{}</style>
<script>var junk=1;</script></head><body>
<nav><a href="/about">About</a></nav>
<main><h1>Big Heading</h1><p>Hello <b>world</b> — text.</p>
<ul><li>alpha</li><li>beta</li></ul>
<article class="post"><h2>Post title</h2><p>Post body with <a href="https://x.nz/deep">a link</a>.</p></article>
</main><footer aria-hidden="true">hidden</footer></body></html>"""

SHELL = ("<!doctype html><html><head>" + "<script src='a.js'></script>" * 6 +
         "</head><body><div id='root'></div></body></html>")

NEXTISH = SHELL.replace("</body>", "<script id=\"__NEXT_DATA__\" type=\"application/json\">" +
                        json.dumps({"props": {"pageProps": {"title": "Hydrated content", "n": 42, "pad": ["x" * 40] * 20}}}) +
                        "</script></body>")


@pytest.fixture
def serve(tmp_path):
    """Serve tmp_path over real http.

    These tests used file:// URLs, which cch-html now refuses — urllib
    honours file:// and ftp://, so an unchecked --url made a read tool into
    a local-file read primitive. Serving over loopback exercises the same
    escalation ladder through the scheme the tool actually supports.
    """
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(tmp_path))
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield lambda name: f'http://127.0.0.1:{httpd.server_address[1]}/{name}'
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_tool(*args, stdin=None):
    p = subprocess.run([sys.executable, str(HTML_TOOL), *args],
                       input=stdin.encode() if stdin else None,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


def test_convert_strips_markup_keeps_content():
    rc, out, _ = run_tool(stdin=PAGE)
    assert rc == 0
    assert '# Big Heading' in out and 'Hello world — text.' in out
    assert '- alpha' in out and '- beta' in out
    assert 'var junk' not in out and '.x{}' not in out
    assert 'hidden' not in out  # aria-hidden pruned
    assert 'a link (https://x.nz/deep)' in out


def test_select_slices_structurally():
    rc, out, _ = run_tool('--select', 'article.post', stdin=PAGE)
    assert rc == 0
    assert 'Post title' in out and 'Big Heading' not in out


def test_shell_detected_and_advised():
    rc, out, _ = run_tool(stdin=SHELL)
    assert rc == 0
    assert 'looks JS-rendered' in out


def test_json_island_extracted(tmp_path, serve):
    (tmp_path / 'shell.html').write_text(NEXTISH)
    rc, out, _ = run_tool('--url', serve('shell.html'))
    assert rc == 0
    assert 'Hydrated content' in out  # island JSON emitted
    assert 'JSON island' in out


@pytest.mark.skipif(not any(shutil.which(b) for b in
    ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')),
    reason='no chrome on box')
def test_render_rung_executes_js(tmp_path, serve):
    (tmp_path / 'dyn.html').write_text(SHELL.replace('</body>',
        "<script>document.getElementById('root').innerHTML='<h1>JS SAYS HI</h1>';</script></body>"))
    rc, out, _ = run_tool('--url', serve('dyn.html'))
    assert rc == 0
    assert 'JS SAYS HI' in out
    assert 'rendered' in out


def test_cache_wrap_converts_curl_html(tmp_path):
    """Provenance-gated: fake `curl` cats HTML; wrapped output must be text."""
    fake_bin = tmp_path / 'bin'; fake_bin.mkdir()
    page = tmp_path / 'page.html'
    page.write_text(PAGE + '<p>pad</p>' * 500)  # push over threshold
    (fake_bin / 'curl').write_text(f'#!/bin/sh\ncat {page}\n')
    os.chmod(fake_bin / 'curl', 0o755)
    env = os.environ.copy()
    env['PATH'] = f"{fake_bin}:{env['PATH']}"
    env['HOME'] = str(tmp_path)
    env['CCH_CACHE_THRESHOLD'] = '2000'
    p = subprocess.run([sys.executable, str(CACHE_WRAP), '--', 'curl https://example.test/'],
                       stdout=subprocess.PIPE, env=env, timeout=60)
    out = p.stdout.decode()
    assert 'Big Heading' in out or 'CCM_CACHED' in out
    assert '<!doctype html' not in out.lower()


def test_cache_wrap_leaves_local_reads_raw(tmp_path):
    """Edit safety: cat of a local html file must NOT be converted."""
    page = tmp_path / 'p.html'
    page.write_text(PAGE)
    env = os.environ.copy(); env['HOME'] = str(tmp_path)
    p = subprocess.run([sys.executable, str(CACHE_WRAP), '--', f'cat {page}'],
                       stdout=subprocess.PIPE, env=env, timeout=60)
    assert '<!doctype html' in p.stdout.decode().lower()
