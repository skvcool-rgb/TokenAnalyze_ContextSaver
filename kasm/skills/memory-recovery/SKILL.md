---
name: memory-recovery
description: kos-memory v5 — primary memory + reality-sync. Use this skill on every project-state or past-context question. Read the SessionStart preamble FIRST (it already has live filesystem state + chunks reconciliation + MEMORY.md anchors) before claiming anything is or isn't built.
---

# memory-recovery — primary memory contract (v5.0+)

This skill replaces the v4.0 "decide when to recall" judgment with a **hard contract** so the user never has to feed you context manually.

## The contract (READ FIRST)

**v5.0 changes everything.** The SessionStart hook already injects, automatically:

1. **MEMORY.md anchors** — operator-curated truth (highest authority).
2. **Live project state** — git branch, head SHA, last 5 commits, tags, dirty files, package versions, top-level tree, test status. All from a fresh subprocess survey.
3. **Recent session catalog** — top sessions in last 30 days with tags + summaries.
4. **Build-status reconciliation** — auto-cross-references chunks claims vs filesystem. Flags `claimed_but_missing` (chunks say built, files don't show it), `built_but_undocumented`, `version_skew`.
5. **Drift warnings** — if MEMORY.md is stale vs latest chunks.

You see all of this BEFORE the user's first prompt arrives. **Reading it is non-negotiable**.

When the user asks "what's the status of X" or "did we ship Y", the UserPromptSubmit hook also auto-runs `reality_sync.quick_status_for_topic(X)` and prints a verdict line BEFORE your turn:

```
[reality check] 'X' is BUILT — confirmed by 12 chunks, filesystem evidence, and git (commit abc123).
  evidence: chunks=claimed_built, filesystem=confirms, git=committed (confidence: high)
```

## Authority order for any project-state claim

1. **Live project state** (git + filesystem) — ground truth, present-tense
2. **MEMORY.md anchors** — operator-curated truth, persistent
3. **User-asserted chunks** (`/remember`) — explicit pins, recent
4. **Auto-extracted chunks** — high-recall, may be stale
5. **Your prior beliefs from training data** — least authoritative for THIS project

When sources conflict, the lower-numbered one wins.

## Hard rules (must follow)

**Rule 1 — Never claim "not built" without evidence.**
> If a user asks whether X is built/shipped/done, and the SessionStart preamble or `[reality check]` line shows ANY positive signal (chunks mention, file exists, git commit, tag), do not respond with "X is not built". Instead either:
>   (a) Affirm the build with the evidence you have, or
>   (b) Say "I see signals X was built (cite source) but verify with you — is the current state still what those signals describe?"

**Rule 2 — Reconciliation flags are mandatory disclosures.**
> If `## Build-status reconciliation` lists "claimed but missing" or "version skew" entries that match the user's question, surface them in your reply. Don't paper over them.

**Rule 3 — Live state beats memory.**
> If chunks say "v4.0 shipped" but Live state shows `tags: v5.1.0` and head commit subject mentions v5.1, the live state wins. Memory is for context, not authority on current state.

**Rule 4 — Don't ask for files the preamble already has.**
> Before requesting that the user paste or read a file, check whether the preamble already shows it (MEMORY.md anchors, tree, last commits). The whole point of v5 is the user shouldn't have to manually feed context.

## When to use `/recall` or `recall_project_memory` MCP tool

After v5.0, manual recall is rarely needed because primary mode auto-injects. Reach for it only when:

- The auto-injected catalog mentions a session_id but its content was elided (truncation marker)
- The user's question is about a topic with **zero** signal in the preamble (chunks=silent, filesystem=silent, git=silent) — recall might surface deeper history
- You need to dig into a specific session's full context — use `/recall` with session-id-shaped query

If the SessionStart preamble already answers the question, do NOT call recall — that's wasted budget.

## Throttle awareness (unchanged)

`recall_project_memory` MCP: 5/session, 50/day, $0.50/day. The auto-injected SessionStart preamble does NOT count against throttle.

## Hook signals you'll see (v5.0)

- `[kos-memory PRIMARY] Memory reconstruction (...)` — SessionStart preamble. Always read it.
- `[kos-memory PRIMARY] Auto-recall fired on trigger (...)` — UserPromptSubmit on past-tense triggers. Passages already inline.
- `[kos-memory PRIMARY] Build-status check fired on (...)` — UserPromptSubmit on present-tense status questions. Verdict already inline.
- `[kos-memory BACKUP] ...` / `[kos-memory hint] ...` — only in opt-out backup mode (legacy v4.0 behavior).

## When manual `/recall` IS still useful

After v5.0, manual recall is rarely needed because primary mode auto-injects. Use it only when:

- The catalog mentions a session_id but its content was elided (truncation marker shown).
- You need to dump a SPECIFIC session's full chunks for deep review.
- The user explicitly types `/recall <query>` themselves.

The `/recall` synthesis sections (a)–(e) below still apply when invoked:

**(a) NEW ITEMS** — bullets with source date
**(b) POTENTIALLY STALE** — what current context believes that past content updated
**(c) SUGGESTED UPDATED STATE** — concise integrated paragraph
**(d) UNCERTAINTY** — what's ambiguous; ask the user
**(e) CONTRADICTIONS DETECTED** — `superseded_chunk_ids: [...]` (empty if none)

Conflict resolution: prefer the most recent unless explicitly contradicted by even-newer. User-asserted outweighs auto-extracted.

## What this skill does NOT cover

- How to seed the store. That happens automatically via Stop and PreCompact hooks.
- How to fix actual drift. Operator updates MEMORY.md or runs `/remember`.
- Cross-project recall (`--user` flag). Same as v4.0.
- Cross-machine sync. See `commands/memory-export.md` and `commands/memory-import.md`.
