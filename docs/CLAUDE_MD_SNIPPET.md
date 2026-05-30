## Tool routing (claude-context-hooks)

This environment routes ALL data interaction through Bash so output is
RTK-compressed and large residuals are cached. Built-in `Read`, `Grep`,
`Glob`, `WebFetch`, `Edit`, `Write`, and `NotebookEdit` are blocked at
the hook layer with deny+suggest. The only exception is `Read` of
multimodal files (images / PDFs / notebooks), which has no Bash
equivalent.

**Inspect / search / list / fetch via Bash:**

| Want                          | Use                               |
| ----------------------------- | --------------------------------- |
| Inspect a text file           | `cat PATH` / `head -n N PATH` / `sed -n 'A,Bp' PATH` |
| Search file contents          | `rg -n PATTERN PATH` (with `-C`, `--type`, `-l` as needed) |
| List files                    | `fd PATTERN PATH` / `find PATH -name 'GLOB' -type f` |
| Fetch a URL                   | `curl -sSL URL` (pipe to `rtk html` for HTML→markdown) |

**Navigate code structure via Bash** (requires `crg build` once per repo):

| Want                          | Use                               |
| ----------------------------- | --------------------------------- |
| Locate a symbol               | `cairn-graph --location SYMBOL`   |
| Callers / callees / tests     | `cairn-graph --callers SYMBOL` / `--callees` / `--tests` |
| Repo orientation              | `cairn-graph --summary`           |
| Past decisions about a symbol | `cairn-graph --knowledge SYMBOL`  |

**Edit / write via Bash helpers** (no read-before-edit cost — full file
never enters context):

| Want                          | Use                               |
| ----------------------------- | --------------------------------- |
| Replace a literal string      | `cch-edit.py PATH 'old' 'new'` (errors if not unique; `--all` to override) |
| Replace multi-line content    | `cch-edit.py PATH --old-file F1 --new-file F2` |
| Write a new or full file      | `echo CONTENT \| cch-write.py PATH` or `cch-write.py PATH << 'EOF' ... EOF` |
| Edit a notebook               | `cch-edit.py PATH 'old_source' 'new_source'` (.ipynb is JSON; literal match works) |

`cch-edit` replicates built-in `Edit`'s safety contract: literal-string
match, errors on missing or non-unique `old_string`, atomic write,
unified diff on success. `cch-write` is atomic (temp + rename) and
reads content from stdin so shell escaping is never an issue.

**Use built-in `Read` only for multimodal files** that Bash can't
substitute: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.pdf`,
`.ipynb`, `.svg`. Built-in `Edit`, `Write`, and `NotebookEdit` are
denied unconditionally — use the Bash helpers above.

**When a Bash command's output is large**, the cache wrapper returns a
`[CCM_CACHED]` stub with a key. Retrieve a slice with:

```
ccm-get.py <key> --grep PATTERN     # lines matching regex
ccm-get.py <key> --head N            # first N lines
ccm-get.py <key> --tail N            # last N lines
ccm-get.py <key> --lines A-B         # line range
ccm-get.py <key> --grep PATTERN -C 2 # with context
ccm-get.py <key> --symbol NAME        # function body via graph.db
```

Don't pull the full content. The cache wrapper warns when `--lines`,
`--head`, or `--tail` would return ≥90% of the stub — same anti-pattern
as `--grep "."`. If filtering genuinely cannot serve the need, use
`--grep "." --reason "<20+ chars why>"`.

**Parallel Bash calls are safe — batch freely.** The cache wrapper is
fail-soft: a Bash command's non-zero exit is reported to the harness as
success, so one call's benign failure (e.g. `grep` no-match → exit 1)
never cancels its sibling calls in the same turn. The real exit code is
preserved in-band: an `[exit N]` line on small output, or the stub's
`exit:` field on cached output. So judge failure from `[exit N]` /
`exit:` (and the command's own output) — not the absence of a tool
error. Set `CCH_PROPAGATE_EXIT=1` to restore raw exit-code propagation if
you need it. Wrapper-usage errors (bad argv) still propagate loudly.

**`pkill -f` / `pgrep -f` self-match.** `-f` matches the whole command
line, including the wrapper chain that contains your own pattern, so
`pkill -f forscan_elm.py` kills its own shell (exit 144). Use a PID file
(`echo $! > x.pid; kill "$(cat x.pid)"`) or exclude yourself
(`pgrep -f foo | grep -v $$`).

**Worked example — tracing a code path across multiple files:**

Don't open the entry file and read top-to-bottom. Use the graph to jump
straight to the symbols you need.

```bash
# 1. Orient
cairn-graph --summary                     # repo shape, top symbols

# 2. Locate the entry symbol
cairn-graph --location handle_request     # → src/server.py:142-198

# 3. Read just that function (NOT the whole file)
sed -n '142,198p' src/server.py

# 4. Follow what it calls
cairn-graph --callees handle_request      # → validate, dispatch, render
cairn-graph --location dispatch           # → src/router.py:55-104
sed -n '55,104p' src/router.py

# 5. Verify a constant before reasoning about it
rg -n 'TIMEOUT_MS' src/router.py
```

Anti-pattern: `cat src/server.py` then `cat src/router.py`. The
`_check_bulk_read` block fires on `cat` of code files; even `sed -n`
of a 200-line range when you only need 50 lines around a function
wastes context. Use `--location SYMBOL` first, then narrow `sed -n
A,Bp` to the function range.
