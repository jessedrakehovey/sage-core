This folder is the agent instruction layer - the rules and templates that govern how the agent writes into this vault.

> [!note] Exempt from front matter
> Files named `about.md` and everything under `Wiki/_meta/` are exempt from front matter requirements per the schema contract. This is one of those files.

## Contents

**schema.md** - the wiki front-matter contract. Every `Wiki/` note must satisfy it. The lint tool (`scripts/wiki_tool.py lint`) parses the fenced `types`, `tags` and `routing` blocks in this file directly - **they are machine-read**. To change them, edit the fenced blocks there; the linter and the agent's note writer update automatically.

**mistakes.md** - the error log. Read at every session start (injected by the `SessionStart` hook). One entry per mistake a previous session made, so none is made twice.

**templates/** - approved templates for recurring notes: a wiki note, a person, a client, a vendor, a daily log. Every template produces a draft; nothing client- or vendor-facing goes out without an owner's approval.

## What this folder is for

Agent instructions only. Operational knowledge belongs in `Wiki/` or `Raw/`. This folder is the control plane: how the agent must behave, what outputs are valid, and where everything goes.

## Hard rules (mirrors schema.md)

1. **Never move, rename or delete anything in `Raw/`.**
2. **Never modify `Memory/Daily_Logs/`** - append-only raw capture.
3. **Never hand-edit `Wiki/_meta/`** - rebuilt by `scripts/wiki_tool.py`.
4. **Everything client- or vendor-facing is a draft** until an owner approves the exact message.
5. **Never commit to a price, a date or contract language** without an owner's say-so.
6. **Log every ingest / maintenance run** to `Wiki/_meta/log.md`.
7. **Log every mistake** to `Schema/mistakes.md` before ending the session.
