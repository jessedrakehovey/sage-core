---
name: wiki-ingest
description: Compile pending Raw/ sources into structured Wiki/ notes per Schema/schema.md. Use when new files land in Raw/ or the user says "ingest".
---

# Wiki Ingest

Turn raw source material into compiled, linked wiki knowledge. Raw stays immutable; the wiki cites it.

## Steps

1. **Find pending work:** run `python scripts/wiki_tool.py sources`, then read `Wiki/_meta/sources.md`. If the user named specific files, ingest only those; otherwise pick the pending sources most relevant to current operations (booking/workflow docs before builder-library docs unless told otherwise).
2. **Read the schema first:** `Schema/schema.md` — front matter spec, allowed types/tags, folder routing, naming rules. The schema requires the `obsidian-markdown` skill before writing any note — use it so every note uses proper Obsidian Flavored Markdown (callouts, embeds, properties, block refs), not just plain Markdown.
3. **For each source:**
   - Read the raw file fully.
   - Decide what it contains: one wiki note or several. Extract *concepts the business reuses*, not summaries of the document. A shoot-day checklist may yield a workflow note AND a concept note (e.g., dual-system audio setup).
   - Check `Wiki/_meta/index.md` for existing notes on the same topic — **update an existing note rather than create a duplicate**. Bump its `updated:` date and append the new source to its `sources:` list.
   - New notes: start from `Schema/templates/wiki-note.md`, route per the schema's folder table, fill complete front matter, cite the raw source path in `sources:`.
   - Link related notes with `[[wikilinks]]` both directions where it matters.
   - Contractual or legal language (e.g., license terms, copyright notices): copy exact wording from the source — never paraphrase.
4. **Rebuild + verify:** `python scripts/wiki_tool.py audit` (this also rebuilds `Wiki/_meta/review-queue.md`). Fix every lint error before finishing. Warnings: fix what's quick, report the rest.
5. **Log:** `python scripts/wiki_tool.py log "ingest: <N> sources -> <M> notes (<short list>)"`.
6. **Commit:** `git add` only the new/updated `Wiki/` notes and the `Wiki/_meta/` files the audit regenerated (never `git add -A` — the repo usually has unrelated work in progress), then commit with message `ingest: <summary>`.
7. **Report for review:** all new notes are written as `status: draft`. Point the user at `Wiki/_meta/review-queue.md` and summarize what was added so they can verify and flip notes to `status: verified`. Never set `verified` yourself.

## YouTube transcripts (`Raw/YouTube/`)

These arrive automatically from the owner's unlisted **"personal AI OS"** playlist (`scripts/yt_ingest/`, every 4 h). The source's front matter carries `url`, `channel`, `title`, `published`, `duration`. Captions are auto-generated: expect no punctuation, misheard names and sponsor/outro chatter.

- **Route by topic, not by source.** Use the schema's folder table across all four lanes. Finance/investing → `Wiki/Money/` (`area: money`), health/goals/routines → `Wiki/Personal/`, house/vehicles → `Wiki/Home/`, AI/tool-building → `Wiki/Builder/`, filmmaking/editing/business → the wedding folders. Create the lane folder if it doesn't exist yet.
- **Extract the reusable ideas, not a recap.** Frameworks, rules of thumb, numbers worth remembering, techniques the owner could apply. Merge into an existing note on the topic when one exists. Drop the intro/sponsor/"smash the like button" filler.
- **Attribute claims to the creator** ("per Andrei Jikh, …"). A YouTuber's figures are their claims, not verified facts — never present investment, tax or health claims as advice; the money lane describes, it never recommends a trade.
- **Obsidian formatting:** put a `> [!info] Source` callout near the top with `[Title](url) — Channel, published date`, and embed the video with `![](<url>)` (Obsidian renders YouTube embeds). Quote at most a short line or two from the transcript — paraphrase the rest.
- `sources:` cites the `Raw/YouTube/<file>.md` path, which is what marks the transcript ingested in `Wiki/_meta/sources.md`.

## Rules

- Never modify files in `Raw/` or `Memory/Daily_Logs/`.
- Anything couple-specific found in a source goes to the couple's profile in `Wiki/Couples/` (or is flagged for `Memory/memory.md`), not buried in a workflow note.
- **Client privacy:** keep clients' personal and contact details out of git-tracked notes beyond what is operationally needed.
- If a source is unreadable or contradicts an existing verified note, stop and report instead of guessing; log conflicts in the commit message.
