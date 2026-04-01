# Claude Context Hooks

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey.svg)](https://www.linux.org/)

Lightweight hooks for Claude Code that cache large tool outputs to keep your context window clean. No CLI modifications required.

Extracted from [claude-context-manager](https://github.com/jimovonz/claude-context-manager) -- the standalone components that don't depend on executable patching.

## The Problem

Even with 1M token contexts, large tool outputs pollute your conversation. A single `grep -r` or build log can dump 50k+ tokens of noise that stays in context forever, displacing useful reasoning. Multiply that across a session and you're compacting much sooner than necessary.

## How It Works

PreToolUse hooks intercept Bash, Glob, Grep, Read, and WebFetch calls. The hook **executes the tool itself**, and if the output exceeds a threshold (default 8KB), caches it to disk and returns a compact stub instead:

```
[CCM_CACHED]
~tokens: 12k
lines: 487
[/CCM_CACHED]
Retrieve: ccm-get.py abc123def0 [--grep PATTERN] [--head N] [--lines N-M]
```

The model then retrieves only what it needs via filtered access:

```bash
ccm-get.py <key> --grep "error|warn"     # Lines matching pattern
ccm-get.py <key> --head 50               # First 50 lines
ccm-get.py <key> --tail 20               # Last 20 lines
ccm-get.py <key> --lines 100-200         # Line range
```

Small outputs (under threshold) pass through inline -- no overhead for simple commands.

### Subagent passthrough

Subagent (Task/Agent) tool calls pass through unintercepted. This lets you delegate data-intensive work to subagents without context cost to the main conversation.

## Installation

```bash
git clone https://github.com/jimovonz/claude-context-hooks.git
cd claude-context-hooks
python install.py
```

This copies hooks to `~/.claude/hooks/` and registers them in `~/.claude/settings.json`.

To unregister:

```bash
python install.py --remove
```

### Optional dependencies

```bash
pip install zstandard   # Better compression (falls back to gzip)
pip install tiktoken    # Accurate token counting for context monitor (falls back to char estimate)
```

## Components

| File | Hook | Purpose |
|------|------|---------|
| `intercept-bash.py` | PreToolUse (Bash) | Execute commands, cache large output, detect interactive commands |
| `intercept-glob.py` | PreToolUse (Glob) | Execute file patterns via `fd`/`find`, cache large listings |
| `intercept-grep.py` | PreToolUse (Grep) | Execute via ripgrep, cache large search results |
| `intercept-read.py` | PreToolUse (Read) | Cache large file reads (whitelists config files) |
| `intercept-webfetch.py` | PreToolUse (WebFetch) | Fetch URLs, cache large responses |
| `context-monitor.py` | UserPromptSubmit | Warn at configurable context usage thresholds |
| `ccm-get.py` | -- | Retrieval tool with required filtering |
| `lib/ccm_cache.py` | -- | Content-addressable cache (BLAKE2s dedup, zstd/gzip compression) |
| `lib/common.py` | -- | Shared utilities (subagent detection, command classification) |
| `config.py` | -- | All configuration |

## Configuration

After installation, edit `~/.claude/hooks/config.py`:

```python
# Output thresholds (bytes) -- outputs larger than this get cached
BASH_THRESHOLD = 8000    # ~2k tokens
GLOB_THRESHOLD = 8000
GREP_THRESHOLD = 8000
READ_THRESHOLD = 25000   # ~6k tokens

# Context monitor
CONTEXT_MAX_TOKENS = 1000000         # 1M context window
CONTEXT_WARN_THRESHOLDS = [70, 80, 90]  # Warn at these percentages
```

## Cache management

Cached content lives in `~/.claude/cache/ccm/`:

```
ccm/
  blobs/<hash>.zst    # Compressed content
  meta/<hash>.json    # Metadata (source, access count, pinning)
  index.jsonl         # Append-only audit log
```

Content is deduplicated by BLAKE2s hash. Identical outputs produce the same cache key.

```bash
# List recent cache entries
~/.claude/hooks/ccm-get.py --list

# Show cache statistics
~/.claude/hooks/ccm-get.py --stats

# Show metadata for a key
~/.claude/hooks/ccm-get.py <key> --info
```

## Bypass

Set `CLAUDE_HOOKS_PASSTHROUGH=1` to temporarily disable all interception:

```bash
CLAUDE_HOOKS_PASSTHROUGH=1 claude
```

## Platform

Linux only. The context monitor uses `/proc` for TTY detection. The hooks themselves should work on macOS but are untested.

## Origin

This project is a standalone extraction of the hook and caching components from [claude-context-manager](https://github.com/jimovonz/claude-context-manager). The original project included CLI patching, a thinking proxy daemon, external compaction routing, and session management -- all removed here in favour of a minimal, maintenance-free package.

## License

[MIT](LICENSE)
