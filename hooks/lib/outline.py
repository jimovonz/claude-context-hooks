"""Extractive outline for cached output.

Every element is a verifiable fact — a count, a line number, or text copied
verbatim from the content. Nothing is paraphrased, because the outline is
delivered automatically inside the stub and the model has no signal with
which to distrust it.

Two products:

  sections  Repeated banner lines the producer emitted itself
            (`==== name ====`, `#### name ####`). Composite shell output
            is the most common large-blob shape in practice, and its own
            delimiters are the cheapest possible index — no format
            knowledge, no command parsing.

  profile   Line count, median and max line length. A line count alone is
            misleading: a 7-line blob can hold a single 20k-character
            line, and nothing in `lines: 7` warns that `--lines 4-4` will
            return the whole thing.
"""
import re
import statistics
from typing import List, Optional, Tuple

BANNER_RE = re.compile(r'^\s*([=#*~-])\1{3,}')
STRIP_CHARS = ' \t=#*~-[]|:'
LONG_LINE_CHARS = 2000
MAX_SECTIONS = 12
MAX_LABEL = 44


def _banner_key(line: str) -> Optional[str]:
    m = BANNER_RE.match(line)
    return m.group(1) * 4 if m else None


def _label(lines: List[str], idx: int) -> str:
    text = lines[idx].strip().strip(STRIP_CHARS).strip()
    if not text:
        for nxt in lines[idx + 1:idx + 3]:
            if nxt.strip():
                text = nxt.strip().strip(STRIP_CHARS).strip()
                break
    text = re.sub(r'\s+', ' ', text)
    return text[:MAX_LABEL] if text else '(unlabelled)'


def _sections(lines: List[str]) -> List[Tuple[int, str]]:
    """(line_number, label) for each banner of the dominant delimiter."""
    keys: dict = {}
    for i, line in enumerate(lines):
        key = _banner_key(line)
        if key:
            keys.setdefault(key, []).append(i)

    best = max(keys.items(), key=lambda kv: len(kv[1]), default=None)
    if not best or len(best[1]) < 2:
        return []

    out = []
    for idx in best[1][:MAX_SECTIONS]:
        out.append((idx + 1, _label(lines, idx)))
    return out


def _profile(lines: List[str]) -> str:
    widths = [len(x) for x in lines] or [0]
    median = int(statistics.median(widths))
    longest = max(widths)
    text = f'lines: {len(lines)} · median {median} chars · max {longest}'
    if longest >= LONG_LINE_CHARS:
        where = widths.index(longest) + 1
        text += f' (L{where} is {longest:,} chars — slice it with --chars A-B)'
    return text


def generate_outline(content: str) -> Optional[str]:
    """Outline lines for a cached blob, newline-joined, or None."""
    if not content:
        return None
    try:
        lines = content.splitlines()
        if not lines:
            return None

        parts = []
        sections = _sections(lines)
        if sections:
            shown = ' · '.join(f'L{n} {label}' for n, label in sections)
            total = len(sections)
            suffix = ' …' if total == MAX_SECTIONS else ''
            parts.append(f'sections: {total} · {shown}{suffix}')
        parts.append(_profile(lines))
        return '\n'.join(parts)
    except Exception:
        return None
