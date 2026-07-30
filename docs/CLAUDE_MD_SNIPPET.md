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
| Inspect a text file           | `cat PATH` / `head -n N PATH` / `sed -n 'A,Bp;Bq' PATH` |
| Search file contents          | `rg -n PATTERN PATH` (with `-C`, `--type`, `-l` as needed) |
| List files                    | `fd PATTERN PATH` / `find PATH -name 'GLOB' -type f` |
| Fetch a URL                   | `curl -sSL URL` (large HTML output auto-converts to text via cch-html) |
| Scrape a JS-rendered page     | `cch-html.py --url URL` (auto-escalates: static → embedded JSON → headless render) |
| Slice a page structurally     | `cch-html.py --url URL --select "main article"` (tag / #id / .class / descendants) |
| Read a local HTML file        | `cch-html.py FILE` or pipe to stdin (READ-ONLY view — never use before editing; edit HTML source raw via `cat` + `cch-edit.py`) |
| Project rules by file pattern | drop `.cch/rules/*.md` with `globs:` frontmatter — the rule body auto-appends (once per session) to output of any command touching a matching file |
| Run many commands at once     | pipe one-per-line to `cch-batch.py` (concurrent, one tool call — see below) |
| Remote work over SSH          | `ssh-tool.py open NAME user@host [--password-file PATH] [-A] [-L/-R spec]` then `ssh-tool.py run NAME -- CMD` — persistent multiplexed session (no per-turn reconnect/reauth), still gets cch caching for free since it's a plain wrapped Bash command. `ssh-tool.py --help` for detach/tunnel/copy/list/reset. Plain `ssh`/`sshpass` remain fine for anything one-off. |

**cch-html — when and when not.** It is a READ tool for page *content*.
Use it when you want what a page *says* (articles, docs, listings, any
remote page). Do NOT use it when you need the actual markup — attributes,
meta/OG tags, JSON-LD, exact HTML for citation — fetch raw instead:
`wget -qO /tmp/page.html URL` then read the file (file reads are never
converted). Do NOT use it on any file you may edit (converted text will
not literal-match the source; use `cat` + `cch-edit.py`). And do not
bother for quick membership checks — `curl URL | grep -o STRING` is
already optimal and bypasses conversion naturally. `--select` supports
only tag / `#id` / `.class` / descendant chains — a no-match on fancier
CSS (`>`, `[attr]`, `:pseudo`) means unsupported syntax, not absent
content.


**Navigate code structure via Bash** (requires `crg build` once per repo):

| Want                          | Use                               |
| ----------------------------- | --------------------------------- |
| Locate a symbol               | `cairn-graph --location SYMBOL`   |
| Callers / callees / tests     | `cairn-graph --callers SYMBOL` / `--callees` / `--tests` |
| Repo orientation              | `cairn-graph --summary`           |
| Past decisions about a symbol | `cairn-graph --knowledge SYMBOL`  |
| Blast radius before an edit   | `cairn-graph --impact SYMBOL` (callers:N tests:M files:F) |
| Body + callers + tests at once | `cairn-graph --context-pack SYMBOL` |
| Everything in one file        | `cairn-graph --file-context FILE` |

**Symbol-lookup order:** `cairn-graph --location SYMBOL` FIRST; `rg` only on a
graph miss (and treat a miss as possible graph staleness — `crg update`).
Symbol-shaped greps are answered from the graph at the hook layer anyway, so
going to the graph first just skips the round-trip. Not every repo has a graph:
it builds on first session contact and refreshes hourly, so an empty
`--summary` means still-building, not absent.

**Edit / write via Bash helpers** (no read-before-edit cost — full file
never enters context):

| Want                          | Use                               |
| ----------------------------- | --------------------------------- |
| Replace a literal string      | `cch-edit.py PATH 'old' 'new'` (errors if not unique; `--all` to override) |
| Replace multi-line content    | `cch-edit.py PATH --old-file F1 --new-file F2` |
| Write a new or full file      | `echo CONTENT \| cch-write.py PATH` or `cch-write.py PATH << 'EOF' ... EOF` |
| Replace a whole function      | `cch-edit.py PATH --symbol NAME --new-file F` (span from the graph — never retype the old body) |
| Apply a multi-hunk patch      | `git apply -` with a unified diff on stdin |
| Edit a notebook               | `cch-edit.py PATH 'old_source' 'new_source'` (.ipynb is JSON; literal match works) |

`cch-edit` replicates built-in `Edit`'s safety contract: literal-string
match, errors on missing or non-unique `old_string`, atomic write,
unified diff on success. **Prefer `--symbol NAME` for whole-function
rewrites**: reproducing the current body as `old_string` pays for it twice,
once as input and again as output, and output tokens are the expensive
ones. Writes follow symlinks to the real file. `cch-write` is atomic (temp + rename) and
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
ccm-get.py <key> --grep PATTERN -C 2 # with context (grep windows over-long
                                     #   matched lines and reports c-offsets)
ccm-get.py <key> --symbol NAME        # function body via graph.db
ccm-get.py <key> --chars A-B         # character range (for very long lines)
```

Stubs for code files include a `symbols: name A-B · ...` menu — prefer
`--symbol NAME` over guessing line ranges. Every stub also carries an
extractive outline: a `sections:` line when the output labelled its own
sections (`==== name ====`), and a `lines: N · median M chars · max X`
profile. Read the profile before slicing — a low line count with a huge
max means one line holds everything, and `--lines` on it returns the lot;
use `--chars A-B` there instead. Small whole-code-file reads may
return inline under a `[CCM_PASSTHROUGH ...]` header instead of a stub; the
header shows the remaining budget for the 5h window (default 25k tokens,
`CCH_PASSTHROUGH_BUDGET`). The budget makes full-file reads a finite
resource — spend it on files you are about to rewrite, not on surveys.

Don't pull the full content. The cache wrapper warns when `--lines`,
`--head`, or `--tail` would return ≥90% of the stub — same anti-pattern
as `--grep "."`. If filtering genuinely cannot serve the need, use
`--grep "." --reason "<20+ chars why>"` — this now spends from the same
finite full-content budget as passthrough, and says no when it is empty
(`ccm-get.py --budget` shows the balance). Housekeeping:
`ccm-get.py --prune` evicts unpinned entries past the TTL / size cap.

**Never spend a whole turn on a retrieval.** A `ccm-get.py` call on its own
costs a full round trip; folded into the next `cch-batch` alongside the work
you were going to do anyway, it costs nothing extra. Measured: half of all
retrievals pull back ~90% of the cached content, so if you can tell you will
need most of it, prefer re-running a narrower command over stub-then-fetch.

**Repeated output collapses.** When a command's output is byte-identical to
its own earlier output in this session you get a one-line
`[CCM_UNCHANGED <key>]` instead of the content; when it changed you may get
`[CCM_DELTA <key>]` with a unified diff against the previous emission. Both
name a key, so `ccm-get.py <key>` still produces the full text if the
earlier copy has fallen out of context.

**Parallel Bash calls are safe — batch freely.** The cache wrapper is
fail-soft: a Bash command's non-zero exit is reported to the harness as
success, so one call's benign failure (`grep` no-match, `ls` missing
path) never cancels its sibling calls in the same turn.

**Because of that, a Bash tool call succeeding does NOT mean the command
succeeded.** A real failure (failed `pytest`, `gcc` error) also reports
success to the harness. Judge success from the output itself and the
in-band exit code: an `[exit N]` line on small output, or the stub's
`exit:` field on cached output. No `[exit N]` / `exit: 0` = the command
returned 0.

(Reference: `CCH_PROPAGATE_EXIT=1` restores raw exit propagation;
wrapper-usage errors like bad argv still propagate loudly.)

**`pkill -f` / `pgrep -f` self-match.** `-f` matches the whole command
line, including the wrapper chain that contains your own pattern, so
`pkill -f forscan_elm.py` kills its own shell (exit 144). Use a PID file
(`echo $! > x.pid; kill "$(cat x.pid)"`) or exclude yourself
(`pgrep -f foo | grep -v $$`).

**`cch-batch.py` — run many independent commands concurrently in ONE
tool call.** Pipe commands (one per line, blanks and `#`-comments
skipped) to `cch-batch.py`; it runs them in parallel and emits one
`===[ cch-batch i/n ]=== <cmd>` block per command, in input order.
Nothing can cancel a sibling because it is a single call, and each
command is shelled through the cache wrapper (large outputs become their
own `[CCM_CACHED]` stubs; exit codes stay in-band). `--jobs N` caps
concurrency (default 8); `--no-cache-wrap` uses plain `bash -c`.

```bash
cch-batch.py << 'BATCH_EOF'
rg -n TODO src/
fd -e py tests/
git log --oneline -5
BATCH_EOF
```

**Same-file guard (built in):** multiple `cch-edit.py`/`cch-write.py`
commands targeting the SAME file in one batch are auto-serialized in
input order (marked `[cch-batch: same-file guard …]` in the output), so
batching many edits to one file is safe. Different files still run in
parallel. Only writers serialize — a reader (`rg`/`cat`) of that file in
the same batch may see pre-edit content. Note the input is still
line-oriented: multi-line quoted args cannot be batched; use
`cch-edit.py --old-file/--new-file` for multi-line edits.

**Batched commands are guarded too.** The bulk-read block, the graph answer
and the `rg -r` warning apply to every line of a batch exactly as they do to
a normal Bash call — batching is a way to spend fewer tool calls, not a way
around the routing rules. Any block can be overridden by re-running the
identical command once.

**Reading code:** never `cat` a code file top-to-bottom — the
`_check_bulk_read` block fires on `cat` of code files. Get the range with
`cairn-graph --location SYMBOL`, then `sed -n 'A,Bp;Bq'` narrowed to the function (the `;Bq` makes sed quit at line B instead of reading to EOF — 125x faster on a large file).
Even a 200-line `sed` window when you need 50 lines around a function wastes
context.
