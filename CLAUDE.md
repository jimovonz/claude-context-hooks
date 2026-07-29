# claude-context-hooks

**State: v2.1.0 shipped 2026-06-26.**
Tagged `v2.1.0` (builds on `v2.0.0` at `e192fa6`), repo
[`jimovonz/claude-context-hooks`](https://github.com/jimovonz/claude-context-hooks).
Installed locally and operational.

v2.1.0 adds: installer auto-provisions its binary deps (RTK, ripgrep, fd)
rather than assuming them; `intercept-grep.py` / `intercept-glob.py` choose
their redirect at runtime (`rg`/`fd` when present, else POSIX `grep`/`find`)
so the suggestion never dead-ends; Agent hook routes more code-structure
prompts to `cairn-graph`.

## What this is

Lightweight Claude Code hook layer that minimises tool-output context cost
by **routing all data interaction through a single Bash data path**. Built-in
tools (`Read`, `Grep`, `Glob`, `WebFetch`, `Edit`, `Write`, `NotebookEdit`)
are blocked at the hook layer; `Read` of multimodal extensions is the only
allowed built-in path because Bash has no equivalent. Bash output is
RTK-compressed and large residuals cached for selective slice retrieval
via `ccm-get.py`. Edits and writes go through `cch-edit` and `cch-write`
helpers (Bash-routed) so the read-before-edit guard never fires.

Coexists with [RTK](https://github.com/rtk-ai/rtk) and
[Cairn](https://github.com/jimovonz/cairn). Personal-use focus.

## Source of truth

[`docs/DESIGN.md`](docs/DESIGN.md) — purpose, single-data-path architecture,
why built-ins are blocked (including the read-before-edit token tax that
forces writes to Bash), routing policy, components, non-goals, open
questions. Read it before changing direction.

## Where we are right now

- v2.1.0 tagged on `main` (v2.0.0 was `e192fa6`); the post-v2 review pass
  (`feature/review-fixes-and-levers`, 2026-07-30) is merged on top. 317 tests
  pass.
  - **Fixed:** `cch-edit`/`cch-write` replaced symlinks and silently lost the
    edit (this repo installs its hooks *as* symlinks); the warning prefix in
    `intercept-bash` executed `$(...)` from a path token; `install.py` wiped
    `settings.json` whole on a JSON parse error (now fatal, refuses to write).
  - **Closed:** `cch-batch` bypassed every guard; `PASSTHROUGH_MARKERS` matched
    substrings; `_check_bulk_read` saw only the first pipe segment;
    `_SESSION_MARKER` was dead so "rerun to override" was false.
  - **Added:** shared `lib/guards.py`, `lib/atomic.py`, `lib/budget.py`,
    `lib/delta.py`, `lib/outline.py`, `lib/supersede.py`; cache TTL/size
    pruning; `cch-edit --symbol`; session ids on every event; honest
    `cch-gain` accounting; `cch-eval.py` compression-fidelity harness.
  - **Retained from the v2.1.0 line:** binary-dep provisioning in `install.py`
    (`--skip-rtk`, `--skip-search-tools`), runtime `rg`/`fd` vs `grep`/`find`
    choice in the Grep/Glob hooks, cairn-graph routing in the Agent hook, and
    `ssh-tool.py` (persistent multiplexed SSH, registered in both file lists).
  - Merge note: `install_instructions()` keeps this line's `rstrip("\n")`
    normalization inside the review pass's `atomic_write_text` — taking the
    review pass's version verbatim breaks `test_install_idempotent_on_claude_md`
    (its trailing-newline handling is not byte-stable across re-installs).
- Installed locally (reinstalled 2026-07-30 after the merge): 29 symlinks in
  `~/.claude/hooks/` (18 top-level + 11 under `lib/`), 8 helpers on PATH via
  `~/.local/bin/` (`cch-batch`, `cch-edit`, `cch-eval`, `cch-gain`, `cch-html`,
  `cch-write`, `ccm-get`, `ssh-tool`), 9 PreToolUse entries in
  `~/.claude/settings.json`. No `CCH_CACHE_THRESHOLD` in the env block — the
  8000 default is the resolved value, so overriding it would be the regression.
- Live smoke test 2026-05-02 confirmed: RTK rewrite + cache stub +
  `ccm-get.py` slice retrieval + bare helper invocation + deny+redirect
  on Read/WebFetch/Write all work. Edit deny is shadowed by the harness
  read-before-edit guard but net effect (Edit unusable, must use
  `cch-edit.py`) is identical.
- RTK installed locally (v0.38.0, `~/.local/bin/rtk`); RTK's PreToolUse:Bash
  hook ordered before our cache wrapper in `~/.claude/settings.json`.
- Stash `pre-repurpose snapshot of intercept-bash.py changes` (`stash@{0}`)
  still present — pre-v2 snapshot of `intercept-bash.py`. Safe to drop
  with `git stash drop stash@{0}` once you've confirmed v2 is solid.

## Architecture in one breath

Block built-in tools → all data goes through Bash → RTK compresses Bash →
wrapper caches residual large output → `ccm-get.py` retrieves selectively.
Edits and writes routed through `cch-edit` / `cch-write` helpers so the
read-before-edit guard never fires. Cairn untouched.

Two distinct mechanisms, two different jobs:
- **Blocking** (Read/Grep/Glob/WebFetch/Edit/Write/NotebookEdit) is the
  *routing* mechanism. Funnels everything to Bash via deny+suggest.
  Multimodal Read is the only structurally-irreplaceable built-in.
- **Wrapping** (Bash) is the *caching* mechanism. Our PreToolUse:Bash
  hook chains after RTK's: RTK rewrites first (`cat foo.py` → `rtk cat
  foo.py`), our hook rewrites second (adds cache wrapper). Bash runs the
  doubly-wrapped command. Output flows through normal `tool_result` —
  inline if small, stub-with-key if large. No deny-channel abuse.

RTK stays fully installed and we layer additively on its hook. We don't
remove or replace it.

## Immediate next steps

1. Soak with session attribution on — `cch-gain.py` can finally report
   corrections/session, so the ~1-2 acceptance target is measurable for the
   first time. Everything logged before 2026-07 has no session id and is
   excluded from the rates.
2. Watch the delta hit rate (`cch-gain` AVOIDED row). If re-reads collapse
   often, `CCH_DELTA_MIN_BYTES` (default 1000) can come down.
2b. **Test the stub-index hypothesis with `cch-gain.py --outline`.** The
   section outline ships but its value rests on an unverified bet: that an
   index makes the model retrieve less, or more narrowly. The one prior
   instance of that bet — the symbol menu — failed it (`--symbol` reached
   0.4% of filter uses while grep took 38%). Decision rule: if `sections
   index` shows no lower retrieval rate and no lower ret/src than `profile
   only` after a few weeks of real traffic, do not extend the generators.
3. Re-run `rtk discover --since 7` to confirm coverage rose above the 2.9%
   pre-install baseline.
4. Sanity-check README against the v2.1 shape (guards module, budget,
   delta, `--symbol` edits).

## Interface with the Cairn proxy

[`docs/CONTRACT.md`](docs/CONTRACT.md) — CCH and the proxy see different things
by position: CCH sees raw tool output before Claude Code touches it, the proxy
sees the assembled request and the cache breakpoints. Neither can see the
other's view, so CCH produces recoverable stubs and the proxy manages them
across turns. Transport is the filesystem; failure domains stay separate.

## Open questions

- **Cache threshold — RESOLVED with data (2026-07): 8000.** Two analyses,
  and only the second one asks the right question.
  - *Coverage view (wrong objective):* p50 output is 89B, p90 744B, p99
    6271B; the live 6000 threshold cached 1.6% of commands. Tuning to p90
    to "catch ~10%" gives 750 — and is a mistake.
  - *Break-even view (right objective):* over 1023 cached events, **84% are
    retrieved at least once** and retrieval pulls back **64% of the bytes**
    (23.0 MB produced, 14.7 MB still emitted). Caching only pays when
    `original − returned − stub − turn_cost > 0`. At a realistic ~500-token
    retrieval turn, every bucket **below 8KB is net negative** (2–4KB alone:
    −450 kB); 8–16KB breaks even; >64KB is overwhelmingly positive
    (+5.9 MB from 40 events).
  - So the original 8000 default was right, and both the 2000 "tune" and the
    750 p90 value were regressions. A cache threshold is a break-even
    question, never a percentile question.
  - Corollary: at a median of 89B, payload size is not where the tokens go —
    round trips are. Volume belongs on the mechanisms that need NO extra
    turn: delta emission, passthrough, promoted symbol menus, and the graph
    answering symbol-greps at hook time.
- **Threshold may be too high — reopen with visible-cost data.** Today's 8000
  came from costing a retrieval round trip at ~500 tokens. Earlier measurement
  of the actual JSONL round trip put it nearer 138–175 visible tokens, which
  moves break-even to `(60 + 175) / (1 - 0.64)` ≈ 650 tokens ≈ **2.6 kB**, not
  8 kB. The two differ on whether the model's own deliberation counts as part of
  the turn cost. Resolve with `cch-gain --outline` once real traffic accumulates
  rather than by re-deriving; the answer changes the threshold by 3x.
- **CLAUDE.md instruction snippet wording.** Iterate against real use.
  The current snippet covers helpers (`cch-edit`, `cch-write`) and the
  unconditional block on Edit/Write/NotebookEdit; correction rate from
  real sessions will tell us whether the helper syntax needs more
  prominence or worked-example flow.

(Resolved during build: hook chaining protocol — empirically works for
RTK + cch on Bash; intercept-read.py allowlist — multimodal-only with
writes via Bash helpers, no two-strike or reason-gate needed; helpers
on PATH — installer now symlinks `cch-edit.py` / `cch-write.py` /
`ccm-get.py` into `~/.local/bin/` collision-safely so bare
invocation works; gain reporting — `cch-gain.py` ships with
`--dist` for size histogram + threshold trial and `--retrieval`
for per-cache orphan/slice analysis; threshold tuning — settings.json
env block is the canonical override path.)

## Acceptance signal

Routing works when ~1–2 corrections per session is the steady-state rate.
Below that, enforcement is too lax; above, it's too aggressive. Cold-start
sessions naturally see more before the CLAUDE.md routing snippet
internalises.

## Dependencies

Best-effort auto-installed by `install.py` (skippable; falls back gracefully):
- [RTK](https://github.com/rtk-ai/rtk) — downloaded + `rtk init` run, unless `--skip-rtk`
- `ripgrep` + `fd` — via the system package manager, unless `--skip-search-tools`
  (Grep/Glob hooks fall back to `grep`/`find` when absent)

User prerequisite (not auto-installed):
- [Cairn](https://github.com/jimovonz/cairn) with UserPromptSubmit + Stop hooks
- Python 3.10+
- Claude Code with `hookSpecificOutput.updatedInput` support

## Coexistence rules (do not violate)

| Surface                       | Owner                                                                   |
| ----------------------------- | ----------------------------------------------------------------------- |
| `PreToolUse:Bash`             | RTK (rewrite, fires first) → this project (cache wrapper, fires second) |
| `PreToolUse:Read`             | this project (block + multimodal allowlist)                             |
| `PreToolUse:Grep`             | this project (block + redirect to `rg` via Bash)                        |
| `PreToolUse:Glob`             | this project (block + redirect to `fd` via Bash)                        |
| `PreToolUse:WebFetch`         | this project (block + redirect to `curl` via Bash)                      |
| `PreToolUse:Edit`             | this project (block + redirect to `cch-edit` via Bash)                  |
| `PreToolUse:Write`            | this project (block + redirect to `cch-write` via Bash)                 |
| `PreToolUse:NotebookEdit`     | this project (block + redirect to `cch-edit` / `jq` via Bash)           |
| `UserPromptSubmit`            | Cairn (do not touch)                                                    |
| `Stop`                        | Cairn (do not touch)                                                    |
| `PostToolUse`                 | unused (do not introduce without strong reason)                         |

## Don't reintroduce v1's mistakes

v1's caching was right; v1's *application* of caching was wrong:
- Cache was the primary mechanism on every tool path → v2 caches only
  Bash residuals (single path, after RTK compression).
- Output was surfaced through the deny channel → v2 surfaces through
  Bash's natural `tool_result` via wrapper-emitted stub.

If you find yourself wanting to add a per-tool cache for Read, Grep,
etc., or surface anything via `permissionDecision: deny` as a result
channel — stop. That's v1's path.

Don't reintroduce v2's first-pass mistakes either:
- Two-strike Read for "Edit-intent" — leaks: model learns double-tap
  always works, defeats Bash routing. Replaced by the `cch-edit`
  helper which removes the read-before-edit coupling entirely.
- Reason-gate via Read tool input — structurally impossible because
  Read's tool schema has `additionalProperties: false`; constrained
  generation strips unknown fields before the hook ever sees them.
