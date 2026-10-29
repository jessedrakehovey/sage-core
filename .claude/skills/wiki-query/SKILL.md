---
name: wiki-query
description: Answer operational questions from the wiki efficiently — index first, then targeted notes, Raw/ only for verification. Use for any "how do we / what's the procedure / who's the vendor / what's the venue" question.
---

# Wiki Query

Answer from compiled knowledge without flooding context. The wiki exists so you do NOT read Raw/ for routine questions.

## Steps

1. **Search the catalog:** `python scripts/wiki_tool.py search <terms>` (try synonyms if the first query misses), or read `Wiki/_meta/index.md` for a browse.
2. **Open only the matching notes.** Read the few that score highest, follow `[[wikilinks]]` one hop if needed.
3. **Couple / vendor / venue questions:** also check `Memory/memory.md` and the relevant profile note (`Wiki/Couples/<date LastnameA-LastnameB>.md`, `Wiki/Vendors/<Name>.md`, `Wiki/Venues/<Name>.md`) — memory holds context that may not yet be in a compiled note.
4. **Verify at the source only when stakes require it** (contract language, licensing terms, export spec numbers): open the Raw/ file cited in the note's `sources:` front matter and confirm the exact wording.
5. **Answer with citations:** name the wiki note(s) you used. If the note is `status: draft`, say so — draft knowledge has not yet been reviewed by the owner.
6. **Gap found?** If the wiki has no answer but a pending Raw/ source likely does (check `Wiki/_meta/sources.md`), answer from the raw source and flag it as an ingestion candidate.

## Rules

- **Gmail and Google Calendar are the systems of record for live booking and scheduling data** (confirmed dates, recent client messages, calendar invites); the wiki holds procedures and durable project knowledge, not live state. Say which one your answer came from.
- Never make contractual commitments, pricing statements, or turnaround promises based on wiki content alone — those require the owner's explicit approval before going to a client.
