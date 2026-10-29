---
name: wiki-maintain
description: Run the full wiki maintenance loop — coverage manifest, index rebuild, lint, fix, log, commit. Use weekly, after bulk ingests, or when the user says "maintain" or "lint".
---

# Wiki Maintain

The health loop that keeps the wiki trustworthy as it scales.

## Steps

1. **Audit:** `python scripts/wiki_tool.py audit` — rebuilds `Wiki/_meta/sources.md` and `Wiki/_meta/index.md`, then lints everything.
2. **Fix every lint ERROR:**
   - Missing/invalid front matter → complete it per `Schema/schema.md` (infer `created:` from git history: `git log --diff-filter=A --format=%as -- "<file>"`).
   - Disallowed tag/type → correct the note to an allowed one. Do **not** hand-edit the fenced blocks to grow the vocabulary: the schema grows by code. Every type or tag the agent was asked for that the schema lacks is a recorded gap (`python assistant/agent/wiki_writer.py gaps`), and the nightly dream (`assistant/agent/schema_growth.py grow`, run from `dream.py`) adds one asked for twice, or once by an owner in so many words ("make a page type for X"), as a linted, ledgered, undoable line plus its folder and `about.md`. To grow it now, have an owner ask in chat, or run `python assistant/agent/schema_growth.py grow` after the gap is recorded.
3. **Triage WARNINGS:**
   - Unresolved `[[links]]` → fix typos; leave genuine placeholders (they mark notes worth writing) but list them in the log entry.
   - Orphan notes → add a link from the most related hub note, or its folder's `about.md`.
   - Empty `sources:` on drafts → fill if the source is obvious, else leave.
4. **Spot-check 2–3 notes** against their cited Raw/ sources — does the compiled claim still match the source? Mismatch = fix the note, and log it in `Schema/mistakes.md` if an agent wrote the bad claim.
5. **Re-run** `python scripts/wiki_tool.py audit` until lint passes.
6. **Log:** `python scripts/wiki_tool.py log "maintain: <errors fixed>, <warnings remaining>"`.
7. **Commit:** message `maintain: <summary>`.

## Rules

- When repairing or rewriting note bodies, use the `obsidian-markdown` skill (and `obsidian-bases` / `json-canvas` for `.base` / `.canvas` files) — keep notes in proper Obsidian Flavored Markdown, not plain Markdown.
- Never "fix" a lint error by deleting content — front matter problems get repaired, not amputated.
- Never hand-edit `Wiki/_meta/` — fix the underlying notes and rebuild.
- If the same class of error keeps recurring across sessions, add an entry to `Schema/mistakes.md` so future sessions stop making it.
