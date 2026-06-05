---
description: Recover past project context — surface forgotten details from prior sessions
argument-hint: "[query] [--days=30] [--user]"
allowed-tools: ["Bash", "Read"]
---

# /recall — Recover past context

You are about to perform context recovery for the user. This is a deliberate, expensive operation that **the user explicitly invoked** — they need information from prior sessions that the current context window has lost or never had.

## What this command does

1. Builds a catalog of past sessions in this project's `.kos-memory/` store.
2. You read the catalog and pick the 1–4 most relevant sessions for the user's query.
3. Greps those sessions for passages matching the query (with synonyms).
4. Synthesizes a delta-vs-current-context report — NEW items, POTENTIALLY STALE items, SUGGESTED state, UNCERTAINTY, CONTRADICTIONS.

You do NOT dump raw transcripts. You synthesize.

## Steps

### 1. Parse arguments

The user invoked: `/recall $ARGUMENTS`

- If `--user` is present, scope is the cross-project user store (`~/.config/kos-memory/user/`).
- Otherwise scope is this project's `.kos-memory/`.
- `--days=N` overrides the default 30-day window.
- The remaining text is the query.

If the user gave no query, ask them once: "What would you like me to recover from past sessions? (e.g. a topic, file, decision, or just 'last session')"

### 2. Run the recall pipeline (Stage 0 + Stage 1)

```bash
python -m mcp.cli recall_stage_a --query "<query>" --window-days <N> [--user]
```

This returns JSON with:
- `expanded_terms` — Stage 0 local synonym expansion
- `catalog_text` — Stage 1 hierarchy you should read
- `kos_dir` — path to the store

### 3. Review the catalog and pick sessions

Read the `catalog_text`. It lists:
- **Recent (≤30d)**: full entries with session_id, date, top tags, chunk count, file refs, summary
- **Mid (30–180d)**: tag-clustered (tag → list of session_ids)
- **Archive (>180d)**: tag-clustered

Pick **1–4 session_ids** that best match the query. Be selective — more isn't better, more = more synthesis cost.

### 4. Run Stage 2 grep on selected sessions

```bash
python -m mcp.cli recall_stage_b --kos-dir "<kos_dir>" --query "<query>" \
  --terms "<expanded_terms_csv>" --sessions "<session_id_csv>" --window-days <N>
```

This returns JSON with `passages` (capped at 20, sorted by user-asserted + recency).

### 5. Synthesize (Stage 3 — you do this in chat)

Compare the retrieved passages against what the current session already believes. Produce **exactly** these sections:

**(a) NEW ITEMS (not in current context)** — bullets with source date.
**(b) POTENTIALLY STALE IN CURRENT CONTEXT** — what the session believes that past content updated/contradicted.
**(c) SUGGESTED UPDATED STATE** — concise integrated paragraph.
**(d) UNCERTAINTY** — what's ambiguous and needs the user.
**(e) CONTRADICTIONS DETECTED** — `superseded_chunk_ids: [<id>, ...]` (empty if none).

**Rules:**
- DO NOT reproduce raw passage text — synthesize.
- Conflict resolution: prefer the most recent unless explicitly contradicted by even-newer.
- User-asserted passages weight higher than auto-extracted.
- Cite source dates (`[2026-04-15]`) inline so the user can trust the timeline.

### 6. Record contradictions back to the store

If section (e) lists any superseded chunk_ids:

```bash
python -m mcp.cli mark_contradicted --kos-dir "<kos_dir>" --ids "<id1,id2,...>"
```

This is the **only** mutation this command performs on the store.

### 7. End with the recovery footer

```
=========================================================
kos-memory recovery — past <N>d
Sources: <X> passages from <Y> sessions
Tokens: ~<estimate> | Latency: stage1=<ms>ms stage2=<ms>ms
Confirm or correct?
=========================================================
```

## Constraints

- This is a **backup mode** operation. If the current chat already has the answer, say so and skip the pipeline.
- If the catalog is empty (`chunks=0`), tell the user: "No prior memory for this project yet. Use `/remember <fact>` to start the store."
- Throttle: only run the full pipeline once per user invocation. Don't re-run on follow-up clarifications without re-invoking `/recall`.
- Cost guardrail: if `expanded_terms` is empty AND catalog is large, ask the user to narrow the query before running Stage 2.
