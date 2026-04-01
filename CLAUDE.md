# Claude Context Hooks

Standalone hooks for Claude Code that cache large tool outputs to prevent context pollution.
No CLI modifications required - works purely via `settings.json` hook registration.

## What It Does

PreToolUse hooks intercept Bash, Glob, Grep, Read, and WebFetch calls. When output exceeds
a threshold (default 8KB), the output is cached to disk and a compact stub is returned to
context instead. The model retrieves cached content via `ccm-get.py` with required filtering.

## Components

- `intercept-{bash,glob,grep,read,webfetch}.py` - PreToolUse hooks that execute tools and cache large output
- `ccm-get.py` - Retrieval tool with required filtering (--grep, --head, --tail, --lines)
- `context-monitor.py` - UserPromptSubmit hook that warns at configurable context thresholds
- `lib/ccm_cache.py` - Content-addressable cache with BLAKE2s dedup and zstd/gzip compression
- `lib/common.py` - Shared utilities (subagent detection, session state, command classification)
- `config.py` - All configuration (thresholds, context limits, cache settings)

## Working With Cached Output

Filtering is REQUIRED when retrieving cached content:

```bash
ccm-get.py <key> --grep "error|warn"     # Lines matching pattern
ccm-get.py <key> --head 50               # First 50 lines
ccm-get.py <key> --tail 20               # Last 20 lines
ccm-get.py <key> --lines 100-200         # Line range
```

Full retrieval requires justification:
```bash
ccm-get.py <key> --grep "." --reason "editing file, need full context"
```

## Subagent Behavior

Main agent calls are intercepted; subagent (Task/Agent) calls pass through unmodified.

## Install

```bash
python install.py           # Install hooks + register in settings.json
python install.py --remove  # Unregister hooks from settings.json
```

## Configuration

Edit `~/.claude/hooks/config.py` after installation. Key settings:
- `BASH_THRESHOLD` / `GREP_THRESHOLD` / etc. - Output size thresholds (bytes)
- `CONTEXT_MAX_TOKENS` - Context window size for monitor (default: 1M)
- `CONTEXT_WARN_THRESHOLDS` - Warning percentages (default: [70, 80, 90])
