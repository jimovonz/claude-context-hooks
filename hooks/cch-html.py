#!/usr/bin/env python3
"""
cch-html — HTML to readable text, with escalation for JS-rendered pages.

Read-side tool ONLY (provenance rule: never applied to local files you might
edit — a converted view poisons literal-match editing). cache-wrap.py invokes
it automatically for large curl/wget output that sniffs as HTML; it is also a
standalone CLI.

Escalation ladder (cheapest first), applied for --url:
  1. plain fetch + convert          (covers SSR/SSG — most content sites)
  2. embedded JSON island           (__NEXT_DATA__ / __INITIAL_STATE__ / ld+json)
  3. headless-Chrome rendered DOM   (--dump-dom, no node/puppeteer dependency)

A JS-shell page is detected (near-zero text, many scripts) and self-diagnosed
in the output rather than silently converted to junk.

Usage:
  cch-html.py FILE                # convert a saved HTML file
  ... | cch-html.py               # convert stdin
  cch-html.py --url URL           # fetch + full escalation ladder
  cch-html.py --url URL --no-render   # ladder without the Chrome rung
  cch-html.py FILE --select "main article"   # structural slice (simple
                                  # selectors: tag, #id, .class, descendants)
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from html import unescape
from html.parser import HTMLParser

SKIP_TAGS = {'script', 'style', 'noscript', 'template', 'svg', 'canvas', 'iframe', 'head'}
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
BLOCK_TAGS = {'p', 'div', 'section', 'article', 'main', 'header', 'footer', 'nav', 'aside',
              'ul', 'ol', 'table', 'tr', 'blockquote', 'pre', 'figure', 'figcaption',
              'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'dt', 'dd', 'form', 'fieldset'}
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
CHROME_BINS = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')


class Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []  # Node or str
        self.parent = parent


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#root')
        self.cur = self.root
        self.script_count = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.script_count += 1
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        # walk up to the nearest matching open tag (tolerates bad nesting)
        n = self.cur
        while n is not self.root:
            if n.tag == tag:
                self.cur = n.parent
                return
            n = n.parent

    def handle_data(self, data):
        if data:
            self.cur.children.append(data)


def parse(html: str) -> tuple[Node, int]:
    tb = TreeBuilder()
    tb.feed(html)
    return tb.root, tb.script_count


# ---------- selector subset: tag, #id, .class, descendant chains ----------

def _match_simple(node: Node, sel: str) -> bool:
    m = re.fullmatch(r'([a-zA-Z][\w-]*)?(#[\w-]+)?((?:\.[\w-]+)*)', sel)
    if not m or not sel:
        return False
    tag, id_, classes = m.group(1), m.group(2), m.group(3)
    if tag and node.tag != tag.lower():
        return False
    if id_ and node.attrs.get('id') != id_[1:]:
        return False
    if classes:
        have = set((node.attrs.get('class') or '').split())
        if not set(classes.strip('.').split('.')) <= have:
            return False
    return True


def select(root: Node, selector: str) -> list[Node]:
    chain = selector.strip().split()
    if not chain:
        return []

    def descendants(n):
        for c in n.children:
            if isinstance(c, Node):
                yield c
                yield from descendants(c)

    hits = [root]
    for sel in chain:
        nxt = []
        for h in hits:
            for d in descendants(h):
                if _match_simple(d, sel):
                    nxt.append(d)
        hits = nxt
    # drop nested duplicates (keep outermost)
    out = []
    for h in hits:
        p = h.parent
        while p and p not in hits:
            p = p.parent
        if not p or p not in hits:
            out.append(h)
    return out


# ---------- text rendering ----------

def render_text(node: Node, links: bool = True) -> str:
    out: list[str] = []

    def emit(n: Node):
        if n.tag in SKIP_TAGS or n.attrs.get('aria-hidden') == 'true':
            return
        if n.tag == 'br':
            out.append('\n')
            return
        if n.tag == 'img':
            alt = (n.attrs.get('alt') or '').strip()
            if alt:
                out.append(f'[img: {alt}]')
            return
        block = n.tag in BLOCK_TAGS
        if block:
            out.append('\n')
        if n.tag and n.tag[0] == 'h' and n.tag[1:].isdigit():
            out.append('#' * min(6, int(n.tag[1:])) + ' ')
        elif n.tag == 'li':
            out.append('- ')
        start = len(out)
        for c in n.children:
            if isinstance(c, str):
                t = re.sub(r'\s+', ' ', c)
                if t.strip():
                    out.append(t)
            else:
                emit(c)
        if n.tag == 'a' and links:
            href = (n.attrs.get('href') or '').strip()
            text = ''.join(x for x in out[start:] if not x.startswith('\n')).strip()
            if href and not href.startswith(('#', 'javascript:')) and text and href != text:
                out.append(f' ({href})')
        if n.tag in ('td', 'th'):
            out.append(' | ')
        if block:
            out.append('\n')

    emit(node)
    text = unescape(''.join(out))
    text = re.sub(r'[ \t]*\n[ \t]*', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip() + '\n'


# ---------- escalation rungs ----------

def is_shell(text: str, script_count: int) -> bool:
    return len(text.strip()) < 400 and script_count >= 4


def json_islands(html: str) -> str | None:
    """Biggest embedded content-JSON blob, if any (rung 2)."""
    pats = [
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>',
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*[;<]',
    ]
    best = ''
    for p in pats:
        for m in re.finditer(p, html, re.S | re.I):
            blob = m.group(1).strip()
            if len(blob) > len(best):
                try:
                    json.loads(blob)
                    best = blob
                except Exception:
                    continue
    return best if len(best) > 512 else None


def chrome_dump(url: str, budget_ms: int = 8000) -> str | None:
    """Rendered DOM via headless Chrome (rung 3). No node deps."""
    binpath = next((shutil.which(b) for b in CHROME_BINS if shutil.which(b)), None)
    if not binpath:
        return None
    try:
        p = subprocess.run(
            [binpath, '--headless=new', '--no-sandbox', '--disable-gpu',
             f'--virtual-time-budget={budget_ms}', '--dump-dom', url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45,
        )
        return p.stdout.decode('utf-8', 'replace') if p.stdout else None
    except Exception:
        return None


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode(r.headers.get_content_charset() or 'utf-8', 'replace')


def convert(html: str, selector: str | None, links: bool) -> tuple[str, int, bool]:
    root, scripts = parse(html)
    if selector:
        nodes = select(root, selector)
        if not nodes:
            return f'[cch-html: no match for selector "{selector}"]\n', scripts, False
        text = '\n'.join(render_text(n, links) for n in nodes)
    else:
        text = render_text(root, links)
    return text, scripts, is_shell(text, scripts)


def main() -> int:
    ap = argparse.ArgumentParser(description='HTML → readable text (read-side only)')
    ap.add_argument('file', nargs='?', help='HTML file (default: stdin)')
    ap.add_argument('--url', help='fetch URL and run the escalation ladder')
    ap.add_argument('--select', help='simple selector slice: tag, #id, .class, descendants')
    ap.add_argument('--no-links', action='store_true', help='drop (href) suffixes')
    ap.add_argument('--no-render', action='store_true', help='never escalate to headless Chrome')
    ap.add_argument('--render', action='store_true', help='force the Chrome rung immediately')
    ap.add_argument('--quiet', action='store_true', help='suppress the [cch-html: …] header')
    a = ap.parse_args()
    links = not a.no_links

    if a.url:
        if a.render:
            html = chrome_dump(a.url) or ''
            via = 'rendered'
            if not html:
                print('[cch-html: headless Chrome unavailable or failed]', file=sys.stderr)
                return 1
        else:
            html = fetch(a.url)
            via = 'static'
        text, scripts, shell = convert(html, a.select, links)
        if shell and not a.render:
            island = json_islands(html)
            if island:
                if not a.quiet:
                    print(f'[cch-html: JS shell ({scripts} scripts) — emitting embedded JSON island, {len(island)//1024}kB]')
                print(island)
                return 0
            if not a.no_render:
                rendered = chrome_dump(a.url)
                if rendered:
                    text, scripts, shell = convert(rendered, a.select, links)
                    via = 'rendered'
        if not a.quiet:
            note = ' — still looks like a JS shell' if shell else ''
            print(f'[cch-html: {via} {len(html)//1024}kB html → {len(text)//1024}kB text{note}]')
        sys.stdout.write(text)
        return 0

    html = open(a.file, encoding='utf-8', errors='replace').read() if a.file else sys.stdin.read()
    text, scripts, shell = convert(html, a.select, links)
    if not a.quiet:
        note = f' — looks JS-rendered ({scripts} scripts, {len(text.strip())} chars text); retry with --url URL for the render ladder' if shell else ''
        print(f'[cch-html: {len(html)//1024}kB html → {len(text)//1024}kB text{note}]')
    sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
