# claude-context-hooks

Lightweight Claude Code hook layer that minimises tool-output context cost
by routing all data interaction through a single Bash data path. Built-in
`Read`, `Grep`, `Glob`, and `WebFetch` are blocked with narrow allowances;
Bash output is RTK-compressed and large residuals are cached for selective
querying via `ccm-get.py`.

Coexists with [RTK](https://github.com/rtk-ai/rtk) and
[Cairn](https://github.com/jimovonz/cairn).

## Architecture in one breath

Block built-in tools → all data goes through Bash → RTK compresses Bash →
wrapper caches residual large output → `ccm-get.py` retrieves selectively.
Cairn untouched.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

## Install

```
git clone https://github.com/jimovonz/claude-context-hooks
cd claude-context-hooks
python3 install.py
```

The installer symlinks hooks into `~/.claude/hooks/`, registers them in
`~/.claude/settings.json` (appending after any existing PreToolUse:Bash
hook so RTK's rewrite still fires first), and warns if `rtk` is not on
PATH. Updates: `git pull` (symlinks track the working copy).

Then paste [`docs/CLAUDE_MD_SNIPPET.md`](docs/CLAUDE_MD_SNIPPET.md) into
your `~/.claude/CLAUDE.md` so the model knows how to route.

`python3 install.py --remove` cleans up symlinks and settings entries.
`python3 install.py --check` runs pre-flight checks only.

## Components

| Path                          | Role                                                       |
| ----------------------------- | ---------------------------------------------------------- |
| `hooks/intercept-bash.py`     | PreToolUse:Bash — wraps command in cache-wrap.py           |
| `hooks/intercept-read.py`     | PreToolUse:Read — multimodal allowlist + edit-intent retry |
| `hooks/intercept-grep.py`     | PreToolUse:Grep — block + redirect to `rg` via Bash        |
| `hooks/intercept-glob.py`     | PreToolUse:Glob — block + redirect to `fd` via Bash        |
| `hooks/intercept-webfetch.py` | PreToolUse:WebFetch — block + redirect to `curl` via Bash  |
| `hooks/cache-wrap.py`         | Runs the inner command, caches+stubs above threshold       |
| `hooks/ccm-get.py`            | Filtered cache retrieval (--grep/--head/--tail/--lines)    |
| `hooks/lib/ccm_cache.py`      | Content-addressable cache (BLAKE2s, zstd/gzip)             |

## Environment variables

| Var                     | Effect                                                  |
| ----------------------- | ------------------------------------------------------- |
| `CCH_DISABLE=1`         | All hooks pass through (debug escape hatch)             |
| `CCH_CACHE_THRESHOLD`   | Bytes threshold for caching Bash output (default 8000)  |

## Tests

```
python3 -m pytest tests/
```

## Dependencies

User prerequisites (not auto-installed):

- Python 3.10+
- Claude Code with `hookSpecificOutput.updatedInput` support
- [RTK](https://github.com/rtk-ai/rtk) — for command compression
- [Cairn](https://github.com/jimovonz/cairn) — for cross-session memory
- Optional: `zstandard` (better cache compression), `tiktoken` (accurate
  token counts in stubs)

## License

[MIT](LICENSE)
