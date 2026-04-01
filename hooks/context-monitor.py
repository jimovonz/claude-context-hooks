#!/usr/bin/env python3
"""
Context monitor - warns when context usage crosses configured thresholds.
Runs on UserPromptSubmit hook, writes warning to terminal.

Uses tiktoken for accurate token counting when available (pip install tiktoken),
falls back to char-based estimation otherwise.

Configuration: Edit config.py to adjust thresholds or disable.
"""

import json
import os
import sys
from pathlib import Path

# Load configuration
HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))

# Defaults (overridden by config.py if present)
CONTEXT_MONITOR_ENABLED = True
CONTEXT_MAX_TOKENS = 1000000
CONTEXT_WARN_THRESHOLDS = [70, 80, 90]
CONTEXT_CHARS_PER_TOKEN = 2.5
CONTEXT_OVERHEAD_TOKENS = 45000
CONTEXT_MESSAGE_MULTIPLIER = 1.5

# Load from config
CONFIG_FILE = HOOKS_DIR / 'config.py'
if CONFIG_FILE.exists():
    _config = {}
    try:
        exec(CONFIG_FILE.read_text(), _config)
        for key in ['CONTEXT_MONITOR_ENABLED', 'CONTEXT_MAX_TOKENS',
                    'CONTEXT_WARN_THRESHOLDS', 'CONTEXT_CHARS_PER_TOKEN',
                    'CONTEXT_OVERHEAD_TOKENS', 'CONTEXT_MESSAGE_MULTIPLIER']:
            if key in _config:
                globals()[key] = _config[key]
    except Exception:
        pass

# Try to load tiktoken for accurate counting
_tokenizer = None
_using_tiktoken = False
try:
    import tiktoken
    _tokenizer = tiktoken.get_encoding("cl100k_base")
    _using_tiktoken = True
except ImportError:
    pass


def count_tokens(text: str) -> int:
    """Count tokens using tiktoken if available, else estimate from chars."""
    if _tokenizer:
        return len(_tokenizer.encode(text, disallowed_special=()))
    return int(len(text) / CONTEXT_CHARS_PER_TOKEN)


def find_last_compaction(lines):
    """Find index of last compaction summary."""
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if 'isCompactSummary' not in line:
            continue
        try:
            obj = json.loads(line)
            if obj.get('isCompactSummary') is True:
                return i
            content = obj.get('message', {}).get('content', [])
            if isinstance(content, list) and content:
                if isinstance(content[0], dict) and content[0].get('isCompactSummary') is True:
                    return i
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return 0


def extract_content_text(content) -> str:
    """Extract all text from message content for tokenization."""
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            block_type = block.get('type', '')

            if block_type == 'text':
                parts.append(block.get('text', ''))
            elif block_type == 'tool_use':
                parts.append(block.get('name', ''))
                inp = block.get('input', {})
                if isinstance(inp, dict):
                    parts.append(json.dumps(inp, separators=(',', ':')))
            elif block_type == 'tool_result':
                result = block.get('content', '')
                if isinstance(result, str):
                    parts.append(result)
                elif isinstance(result, list):
                    parts.append(extract_content_text(result))
            elif block_type == 'thinking':
                parts.append(block.get('thinking', ''))

    return '\n'.join(parts)


def estimate_context(session_path) -> tuple[float, int]:
    """Estimate current context usage. Returns (percentage, token_count)."""
    try:
        with open(session_path) as f:
            lines = f.readlines()

        last_compact = find_last_compaction(lines)
        recent_lines = lines[last_compact:] if last_compact > 0 else lines

        all_text = []
        for line in recent_lines:
            try:
                obj = json.loads(line)
                msg = obj.get('message', {})
                if not msg:
                    continue
                all_text.append(msg.get('role', ''))
                all_text.append(extract_content_text(msg.get('content', '')))
            except Exception:
                pass

        combined_text = '\n'.join(all_text)
        content_tokens = count_tokens(combined_text)
        adjusted_tokens = int(content_tokens * CONTEXT_MESSAGE_MULTIPLIER)
        total_tokens = adjusted_tokens + CONTEXT_OVERHEAD_TOKENS

        pct = min(100, (total_tokens / CONTEXT_MAX_TOKENS) * 100)
        return pct, total_tokens
    except Exception:
        return 0, 0


def get_crossed_threshold(pct, last_threshold):
    """Find highest threshold crossed that's above last warning."""
    for threshold in sorted(CONTEXT_WARN_THRESHOLDS, reverse=True):
        if pct >= threshold > last_threshold:
            return threshold
    return None


def get_last_warning(state_file):
    """Get last warning threshold from state file."""
    try:
        with open(state_file) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def set_last_warning(state_file, level):
    """Save last warning threshold."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, 'w') as f:
            f.write(str(level))
    except Exception:
        pass


def main():
    if not CONTEXT_MONITOR_ENABLED:
        return

    input_data = json.load(sys.stdin)
    transcript_path = input_data.get('transcript_path', '')

    if not transcript_path:
        return

    session_id = Path(transcript_path).stem
    state_file = Path.home() / '.claude' / 'state' / f'{session_id}-context-level'

    pct, tokens = estimate_context(transcript_path)
    last_threshold = get_last_warning(state_file)
    crossed = get_crossed_threshold(pct, last_threshold)

    if crossed:
        set_last_warning(state_file, crossed)

        max_threshold = max(CONTEXT_WARN_THRESHOLDS)
        high_threshold = sorted(CONTEXT_WARN_THRESHOLDS)[-2] if len(CONTEXT_WARN_THRESHOLDS) > 1 else max_threshold

        if crossed >= max_threshold:
            urgency = "CRITICAL"
            action = "Compaction is imminent."
        elif crossed >= high_threshold:
            urgency = "WARNING"
            action = "Context getting full."
        else:
            urgency = "NOTE"
            action = ""

        method = "tiktoken" if _using_tiktoken else "estimated"
        message = f"{urgency}: Context at {int(pct)}% (~{tokens:,} tokens, {method}). {action}"

        # Write directly to terminal
        tty_written = False
        tty_path = None

        # Try to find Claude's TTY by walking up process tree
        try:
            pid = os.getppid()
            for _ in range(5):
                fd1 = f'/proc/{pid}/fd/1'
                if os.path.exists(fd1):
                    target = os.readlink(fd1)
                    if '/pts/' in target or '/dev/tty' in target:
                        tty_path = target
                        break
                stat = Path(f'/proc/{pid}/stat').read_text()
                ppid = int(stat.split()[3])
                if ppid <= 1:
                    break
                pid = ppid
        except Exception:
            pass

        if tty_path:
            try:
                with open(tty_path, 'w') as tty:
                    tty.write(f"\n\033[1;33m{message}\033[0m\n")
                    tty_written = True
            except Exception:
                pass

        if not tty_written:
            try:
                with open('/dev/tty', 'w') as tty:
                    tty.write(f"\n\033[1;33m{message}\033[0m\n")
            except Exception:
                pass
    else:
        # Track decreases (e.g., after compaction) to reset warning state
        current_max_crossed = 0
        for threshold in CONTEXT_WARN_THRESHOLDS:
            if pct >= threshold:
                current_max_crossed = threshold

        if current_max_crossed < last_threshold:
            set_last_warning(state_file, current_max_crossed)


if __name__ == '__main__':
    main()
