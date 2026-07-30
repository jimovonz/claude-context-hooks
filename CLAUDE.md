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

- v2.1.0's release notes are on `main`, but **no `v2.1.0` tag exists** — the
  only tag is `v2.0.0` at `e192fa6` (`git describe` reads
  `v2.0.0-55-g089d6b0`). PR #3 merged the post-v2 review pass at `089d6b0`,
  subsuming PR #1 (`release/v2.1.0` was an ancestor of it). 338 tests pass.
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
- Live smoke test 2026-07-30 (post-merge) confirmed: Read deny on `.py` and
  allow on `.png`; Bash rewrite through `cache-wrap.py`; the `rg -r` warning
  firing from `lib/guards.py`; `cch-html.py`, `cch-edit.py`, `ssh-tool.py`
  executing from PATH; 4000-line output → stub → `ccm-get --lines` slice;
  identical re-run → `[CCM_UNCHANGED]`; changed re-run → `[CCM_DELTA]`
  emitting 111B against 18898B, with the supersession index written and the
  superseded key still resolving through the chain; and `cch-edit.py`
  writing *through* a symlink without replacing it — the data-loss fix
  verified against its actual failure mode (this repo installs hooks as
  symlinks). Edit deny stays shadowed by the harness read-before-edit guard,
  but the net effect (Edit unusable, must use `cch-edit.py`) is identical.
- RTK installed locally (v0.38.0, `~/.local/bin/rtk`); RTK's PreToolUse:Bash
  hook ordered before our cache wrapper in `~/.claude/settings.json`.

## Two machines, one history

Development runs on two PCs (home and work) and neither sees the other's
in-progress work, so **version-number gaps are expected, not errors** — a
missing intermediate is almost always work that happened elsewhere, or a
number claimed in a status header before the tag was cut.

The invariant that keeps them consistent: **a version exists when its tag is
on `origin`, not when CLAUDE.md says so.** Verify with
`git ls-remote --tags origin`, never with local `git tag -l` (a local-only
tag is invisible to the other machine, which is how a header can claim a
release nothing else can see). Push tags with the commits that earn them:
`git push --follow-tags`.

## Hot-path invariant: keep `lib/guards.py` cheap to import

`PreToolUse:Bash` imports `lib.guards` (and `lib.event_log`) on **every** Bash
call, so a module-scope import there is a tax on every command the model runs.
Measured 2026-07-30: `import hashlib, sqlite3` + `from typing import Optional`
cost 6.4ms per call, ~5ms of which the common path never needed — `sqlite3`
(plus the `datetime` it drags in) is only reached by `graph_answer`, `hashlib`
only by `_override_marker`, which `block()` calls solely once a guard has
already fired. All three are now deferred to their call sites, and annotations
are strings via `from __future__ import annotations` so `typing` is not
imported at all.

Rule: a new import in `guards.py` or `event_log.py` goes at module scope only
if the no-guard-fires path actually uses it. Verify with
`python3 -X importtime hooks/intercept-bash.py < /dev/null`, and A/B against
`git show HEAD:<file>` in one interleaved run — the bare-interpreter baseline
drifts by several ms between runs, so sequential before/after timings lie.

`python3 -S` was measured and **rejected**: `ccm_cache` imports `zstandard`
under `try/except ImportError` with a gzip fallback, so skipping site-packages
would silently degrade compression rather than fail loudly.

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
  - *Considered and rejected: 2.6 kB.* Costing the round trip at only its
    138–175 visible JSONL tokens moves break-even to
    `(60 + 175) / (1 - 0.64)` ≈ 650 tokens ≈ 2.6 kB. That costing is the
    wrong one: it counts the bytes of the retrieval turn but not the
    deliberation the turn provokes, and deliberation is the larger half.
    **8000 stands as the intended value** (2026-07-30, confirmed against the
    local data). Only new `cch-gain --outline` evidence reopens it — not a
    re-derivation from the same numbers.
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
