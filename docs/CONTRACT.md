# CCH ↔ Cairn proxy contract, v1

CCH and the Cairn proxy see different things **by position**, and neither can
see the other's view:

| | sees | cannot see |
|---|---|---|
| **CCH** (PreToolUse hooks) | raw tool output, full, before Claude Code touches it | the assembled request, other turns, the cache prefix |
| **Cairn proxy** (`ANTHROPIC_BASE_URL`) | the whole `messages` array, `cache_control` breakpoints | tool output as it was before Claude Code shaped it |

So CCH is the only component that can *produce* a recoverable stub, and the
proxy is the only one that can *manage* stubs across turns. This document is
the seam. It replaces the current arrangement, where Cairn reaches into CCH
policy through a hardcoded `PARE_TOOLS_CCH_DENIED` list.

**Transport is the filesystem.** Both already write under `~/.claude/cache/`.
No IPC, no imports across repos, no shared process. The proxy reads files; if
they are absent it does nothing. That keeps the failure domains separate —
CCH works with the proxy off, the proxy works with CCH uninstalled.

---

## 1. Stub grammar (CCH publishes, proxy matches)

```
<promoted lines>          ← optional: cairn-graph footer, symbols:, sections:, lines:
[CCM_CACHED]
~tokens: <n>
lines: <n>
exit: <n>                 ← present only when non-zero
check: <4 hex>
[/CCM_CACHED]
Retrieve: ccm-get.py <key> [--symbol NAME] [--grep PATTERN [-C N]] …
```

Match on `[CCM_CACHED]` … `[/CCM_CACHED]` followed by a `Retrieve:` line. The
key is on the `Retrieve:` line, never inside the block. Promoted lines sit
**above** the block and are part of the stub for replacement purposes.

Stable within v1. Additions go above the block or as new fields before
`check:`; nothing already present is removed or reordered.

## 2. Key scheme

`b2s:<16 lowercase hex>` — BLAKE2s-64 of the exact UTF-8 content.

Content-addressed, so identical content yields an identical key across
sessions, projects, and machines. **The proxy may treat key equality as
content equality** without reading the blob.

## 3. Supersession index

```
~/.claude/cache/cch/superseded/<hex-of-old-key>   →  contains the newer key
```

Written by CCH when the same `(session, command)` signature produces a
different content key — i.e. the same command was re-run and the world
changed. Chains resolve transitively (`K1 → K2 → K3`); resolution is capped
and cycle-safe.

**Monotone: an entry is never rewritten to a different value.** That is what
makes the proxy's transform deterministic, and determinism is the property
that matters — not faithfulness. A transform that flips between requests
breaks the cached prefix every time it changes its mind.

## 4. The elision predicate

The proxy may replace a full tool result with its stub **only** when all hold:

1. the content for `key` is retrievable from the CCM cache;
2. `key` is superseded, per §3;
3. the superseding key (or a later one in its chain) is **already present in
   the current `messages` array**.

Clause 3 is the whole safety argument. Eviction is unsafe in general because
the model cannot know what it is missing — but superseded content is the one
case where nothing is missing: the current version is already in front of it.
No recognition is required, so none can fail.

Reference implementation: `hooks/lib/supersede.py` (`may_elide`). The proxy
may import it or reimplement from this document; the on-disk format is the
contract, not the code.

**Reachability, measured.** The predicate is sound and almost never true. Over
62,747 wrapped commands, supersession fired **twice**; only 3.9% of attributed
(session, command) signatures were re-run at all, for 88 extra runs in total.
Sizing the wider opportunity did not rescue it either: the 1–8 kB band the
proxy could otherwise elide holds real volume (23k tokens in a single
150-command session, ~25k projected for 500 commands), but payback depends on
the history *after* the elision point, not on how much is elided — 7.6 further
requests when 10k tokens follow, 38 when 50k do, 76 for a small elision deep in
a session. Low-`H` sites are recent and probably still in use; high-`H` sites
are safe and unaffordable. The one free moment is compaction, and it belongs to
Claude Code: the proxy sees only the compacted result, and cannot know which
request is the last before it.

So §4 and §5 are specified and implemented on CCH's side, and the proxy half is
**deliberately not built**. Build it if traffic ever makes the predicate fire;
do not build it on the strength of the design reading well.

## 5. Placement rules

**Elide in place, once.** Replacing a full result with its stub changes the
prefix at that position, costing one cache rebuild — then it is stable
forever, because supersession never reverses. Prefer to apply it at a
compaction boundary, where the rebuild is already being paid for.

**Re-expand at the tail, never in place.** If the proxy later judges that
elided content is relevant again — headroom's proactive expansion, matching a
new user turn against what was compressed — it must inject the retrieved
content **after the last `cache_control` breakpoint**, the placement
`request_inject` already uses for per-prompt content. Un-eliding in place
would invalidate the prefix a second time and make the transform
non-deterministic.

## 6. Obligations

**CCH promises**
- the stub grammar and key scheme are stable within v1;
- a key is never reused for different content;
- cached content stays retrievable for `CCH_CACHE_TTL_DAYS` (default 14);
  pinned entries are never pruned;
- `may_elide` is monotone for a given `(key, present-keys)` input.

**The proxy promises**
- it elides only where `may_elide` holds;
- its transform is deterministic — same messages plus same index yields the
  same output, byte for byte;
- it fails open: any error returns the body unchanged;
- it never re-expands in place.

## 7. Out of scope, deliberately

- **Fabricated tool_use/tool_result pairs.** Claude Code owns its tool loop;
  a pair only the proxy knows about makes CC's transcript disagree with the
  model's context, which breaks compaction, `--resume`, and Stop-hook capture.
- **Recency-based eviction.** Position is not evidence that content stopped
  mattering; a spec read at turn 3 can be load-bearing at turn 40.
- **Abstractive summarisation in stubs.** Stub content is extractive only —
  counts, offsets, verbatim excerpts. A stub is consumed unconditionally, so a
  paraphrase that misstates the content suppresses the retrieval that would
  have caught it.

## 8. Taken from headroom

| Idea | Adopted as |
|---|---|
| CCR — compress, cache, retrieve; originals never deleted | already CCH's design; §2 pins the key scheme |
| CacheAligner — stable prefix, volatile content to the tail | §5, using Cairn's existing breakpoint relocation |
| Context Tracker — proactive expansion on a new query | §5 re-expansion, but tail-injected to stay deterministic |
| Multi-factor eviction, dropped messages recoverable via CCR | §4 — narrowed to supersession, which needs no scoring |
| BM25 `query=` retrieval instead of offsets | `ccm-get --grep`, plus the `sections:` index |

Not adopted: LLMLingua-class ML compression (2 GB of dependencies against a
stdlib-only project), and telemetry-on-by-default.
