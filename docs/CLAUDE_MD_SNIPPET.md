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

Don't pull the full content unless filtering genuinely cannot serve the
need (`--grep "." --reason "<20+ chars why>"`).
