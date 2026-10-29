---
name: dreaming
description: Morning consolidation — extract durable facts from yesterday's Memory/Daily_Logs/ into Memory/memory.md, then run a self-improvement scan that proposes skill/mistake patches and new automations. Runs as the 6:30 AM daily cron, or on demand with "dream" / "consolidate memory".
---

# Dreaming — Daily Consolidation & Self-Improvement

Three phases. **Phase 1** promotes durable facts from raw daily logs into the persistent memory file. **Phase 2** reads the same logs for signs the agent should *learn* — auto-applying low-risk fixes and filing consent-first proposals for the rest. **Phase 3** ingests up to 3 pending `Raw/` sources into draft wiki notes each night. Run all three every time.

# Phase 1 — Memory consolidation

Promote durable facts from raw daily logs into the persistent memory file agents read before any client, vendor, or project work.

## Steps

1. **Find unprocessed logs:** list `Memory/Daily_Logs/`, read `Memory/memory.md`'s "Last consolidated" line, and process every log file dated after it (normally just yesterday's).
2. **Extract only durable facts** — things that will matter in 30+ days:
   - Couple/client: communication preferences, must-have moments, family/logistics sensitivities, names of key people, special requests
   - Vendor/collaborator: reliability patterns, scheduling habits, who plays well with whom, preferred contact method
   - Venue: access/load-in, lighting, AV/sound feed, parking, restrictions (drones, open flame, hours)
   - Project status: current stage, what's blocking the next step, promised delivery date, revision round
   - Decisions: pricing/package/workflow calls the owner made and the reasoning
   - Open threads: active situations that span days (an edit in progress, an upcoming shoot, an unresolved client question)
3. **Skip the ephemeral:** one-off scheduling, resolved same-day items, anything already closed out.
4. **Write to `Memory/memory.md`:** under the matching section, dated, replacing any line it supersedes (memory is current-state, not a journal — the daily log keeps the history). Update the "Last consolidated" date. This is an Obsidian note — use the `obsidian-markdown` skill so links to couple/vendor/venue profiles use `[[wikilinks]]` and callouts/properties render correctly.
5. **Cross-promote:** facts about a couple with a `Wiki/Couples/` profile also get added to that profile (bump `updated:`). Vendor facts → `Wiki/Vendors/` profile. Venue facts → `Wiki/Venues/` profile.
6. **Never modify the daily logs themselves.**
7. **Commit:** message `dreaming: consolidated <dates>`.

## Rules

- Record events and facts, never characterizations of couples or clients. Weddings are emotional; keep memory factual and kind.
- When a new fact contradicts an existing memory line, the new fact wins — but note the change ("was X until YYYY-MM-DD").
- If a log mentions anything high-stakes — **lost or corrupted footage, a missed moment, a backup that failed verification, a contract/payment dispute, a refund, or a slipped delivery date** — make sure it's flagged in memory under "Active situations." These must not get lost.

# Phase 2 — Self-improvement (auto-apply, graduated)

After Phase 1, re-read the same logs through a different lens: **did this session reveal a way the agent should work differently next time?** Two more inputs carry the same signal since 2026-09-30 and are read here too: `assistant/data/cards/answers.jsonl` (every card the owner answered; a typed `text` or a `revised_text` is a correction) and `assistant/learnings/*.md` (per-lane raw corrections + distilled lessons). A pattern across several corrections is the strongest evidence for a skill patch there is. This is the Hermes "background review" closed-loop. It **applies** its own low-risk improvements and **gates** the high-stakes ones for the owner.

> [!important] The graduated auto-vs-gated split
> | Change | Action |
> |---|---|
> | Append to `Schema/mistakes.md` | **AUTO-APPLY** |
> | Patch a `SKILL.md` step / recipe / pitfall / routing detail | **AUTO-APPLY** |
> | New scheduled automation / cron job | **PROPOSE** → `Memory/automation-suggestions.md` |
> | Anything touching the **protected core** (below) | **PROPOSE** → `Memory/skill-suggestions.md` |
>
> Every auto-applied change is committed to git (the rollback point) and the 6:30 AM cron posts a Mission Control note with exactly what changed, so self-improvements are reviewable/revertible. Telegram was removed 2026-10-04; Mission Control is the only channel. Memory facts (Phase 1) stay auto-written. The **"never send a client/vendor message without the owner's approval"** rule is untouched by all of this.

## The protected core — NEVER auto-edit (propose instead)

A patch is **protected** — route it to `Memory/skill-suggestions.md` as a gated proposal and do **not** apply it — if it would touch any of:

- **Footage-handling safety** — any rule about card-offload sequencing, the 3-2-1 backup scheme, verifying backups *before* formatting a card, or never deleting/moving originals. The footage is irreplaceable; these rules never auto-change.
- **Send-gating** — any "draft-only / never send / never submit / gated on the owner's approval" rule, or a poll's tool allowlist that enforces it.
- **Client-facing commitments** — exact contract language, pricing, package deliverables, or promised turnaround / revision-round counts that go to couples.
- **These guardrails themselves** — this Phase 2 logic, the protected-core list, or the auto-vs-gated split.

When in doubt whether a change is protected, treat it as protected and propose it.

## What to look for

Be **active** — most working sessions leave at least one signal. Scan the logs for:

- **Corrections & frustration** — "stop doing X", "too verbose", "just give me the answer", "don't word it like that", or an explicit "remember this." These are **first-class skill signals**: patch the *governing skill* so the next session starts already knowing. Name that skill.
- **Workflow corrections** — the owner changed the agent's approach, order of steps, or a routing/vendor/tone call. Encode it as a pitfall or explicit step in the governing skill.
- **Reusable technique** — a non-trivial fix, browser recipe, export setting, or tool-usage pattern emerged that a future session would benefit from. Capture it.
- **Wrong/incomplete skill** — a skill that got loaded this session turned out stale, missing a step, or misleading. Patch it.
- **Un-logged mistake** — the agent made an error not yet in `Schema/mistakes.md`.
- **Recurring manual ask** — the owner asked for the same kind of work on a repeating cadence (e.g. drafted the same inquiry replies several times). A scheduled job would serve it.
- **Unscripted recurring work (Skill Radar)** — work the owner drove **by hand** that **no installed skill covers** and that has **recurred across 2+ distinct days** — the signature of a task worth turning into a skill/workflow. The detection is pre-computed by a deterministic token-free prefilter; your job is judgement, not the search. See the dedicated Skill Radar step below.

## Skill Radar step

1. Read `scripts/skill_radar/candidates.json` (prefilter output — clusters of recurring work with no name-matched skill) and `scripts/skill_radar/dismissed.json` (latched dismissals). The dream cron refreshes `candidates.json` each night by running `python scripts/skill_radar/detect.py` before this skill loads; if the file is missing or stale, run that command yourself first — it is deterministic and token-free.
2. For each candidate whose `key` does **not** appear in `dismissed.json`, judge:
   - **Already covered** — an existing skill really does this job and the prefilter's term-match missed it (check `nearest_skill`). → Ignore, don't propose.
   - **One-off / resolved / dev-infra** — it recurred but isn't durable operational work. → Ignore.
   - **Better as an extension of an existing skill** — `nearest_skill` covers most of this and the gap is a missing step, not a whole workflow. → Propose `Target: extend <skill>` in `Memory/skill-suggestions.md` (Pending).
   - **Genuine unmet operational need** → Write a `Target: new skill: <name>` entry to `## Pending` in `Memory/skill-suggestions.md`. Use the candidate's `key` verbatim in the `###` header (`### key: <sr-key>   <Title>`). Use `days` + `sample_prompts` as Evidence. **A new skill is always Pending (gated), never auto-built.**
3. Cap new-skill/extend proposals at 3 per night. Honor `dismissed.json` strictly — never re-propose an in-force dismissed key.

## What to do with each signal

1. **Un-logged mistake / correction that belongs in the mistakes log → append to `Schema/mistakes.md`** directly, in that file's existing entry format. Dedupe against existing entries first.
2. **Low-risk skill fix (a step, recipe, pitfall, routing/tone detail, tool-usage note) that is NOT protected → edit the target `SKILL.md` directly.** Make the smallest change that encodes the lesson, then log it in the audit trail (below).
3. **Protected-core skill/mistakes change → `Memory/skill-suggestions.md`** (Pending — needs the owner). Propose, never apply. Use the file's proposal format; dedupe against every section incl. **Dismissed**.
4. **Recurring-ask / blueprint (new automation) → `Memory/automation-suggestions.md`.** Propose only — consent-first, never auto-schedule. Dedupe + **honor latched dismissals**; cap Pending at 5.
5. **Skill Radar candidate** — see the Skill Radar step above.
6. **A wiki type or tag the schema lacks?** Never hand-edit the fenced blocks in `Schema/schema.md`. The agent's nightly pass (`assistant/agent/dream.py` → `schema_growth.grow`) already turns a recorded gap (`python assistant/agent/wiki_writer.py gaps`) asked for twice, or once by an owner in so many words, into a linted, ledgered, undoable schema line; leave it to that step.
7. **Nothing qualifies?** Change nothing. A genuinely quiet day is fine — but treat "found nothing" as the exception, not the default.

## Auto-apply safety rails

- **Evidence required.** Every auto-applied change cites the log date and quotes/paraphrases the triggering line. No evidence → don't touch anything.
- **Confidence gate.** Only auto-apply a change that is **clear and unambiguous**. If you're unsure what the right fix is, or it could change behavior unpredictably, **propose it** (skill-suggestions.md) instead — never guess into a live skill.
- **Cap: at most 3 auto-applied `SKILL.md` patches per night.** Beyond 3, queue the rest in `skill-suggestions.md` and note the backlog. (mistakes.md appends aren't capped, but must be deduped.)
- **Smallest viable edit.** Patch the specific step/section; never rewrite a whole skill.

## Audit trail — so the owner can review what the loop did to itself

For **every auto-applied change**, append a one-block entry to the `## Applied automatically` section of `Memory/skill-suggestions.md`: date, target file, one-line what-changed, the evidence quote. That file is the single place the owner reviews the loop's self-edits; git holds the exact diff for rollback.

# Phase 3 — Wiki ingest (bounded, wedding file types)

After Phases 1-2, compile pending `Raw/` sources into draft wiki notes so nothing the owner drops into `Raw/` sits uncompiled. The coverage tracker is **file-type-agnostic** — PDFs, Word docs, PowerPoints, and transcripts are sources just like markdown.

## Steps

1. **Find pending sources:** run `python scripts/wiki_tool.py sources`, then read `Wiki/_meta/sources.md`. The `**pending**` rows are the work.
2. **Pick this night's batch — BOUNDED.** Ingest **at most 3 sources per night** (cost rail, same spirit as Phase 2's 3-patch cap). Choose the highest-value pending sources: `Raw/YouTube/` transcripts first (the owner queued them on purpose, so they must not starve behind a backlog), then wedding operational docs (packages, venue/vendor contracts, shot lists, workflow guides) before AI-reference material. The rest roll to tomorrow — the backlog drains a few per day.
3. **Run the `wiki-ingest` skill on exactly those sources.** It handles per-type text extraction (`.pptx`/`.docx`/`.xlsx` via the Office skills, `.pdf` via the pdf skill) and writes schema-valid draft notes. Route each new note by topic to the correct subfolder per `Schema/schema.md`, across all four lanes: `Couples/`, `Vendors/`, `Venues/`, `Packages/`, `Post-Production/`, `Operations/`, `Integrations/`, `Builder/`, or `People/`, `Personal/`, `Home/`, `Money/`. `Raw/YouTube/` transcripts (from the owner's "personal AI OS" playlist) follow the skill's YouTube section: obsidian-markdown formatting, embedded video, reusable ideas rather than a recap. Follow `wiki-ingest` fully, including `python scripts/wiki_tool.py audit` — **0 lint errors before committing.**
4. **Skip non-ingestable inputs:** non-textual media (`.m4a`, `.mp4`, images with no text) and blank fillable forms/templates — note them as out-of-scope rather than forcing a note. Never fabricate content from a file you couldn't read.
5. **Drafts only.** Every new note is `status: draft` and surfaces in `Wiki/_meta/review-queue.md` for the owner — never set `verified` yourself.

## Phase 3 safety rails

- **Cap: at most 3 sources ingested per night.** Log the remaining backlog count.
- **All new notes are drafts** — review-gated, exactly like the on-demand skill.
- **Same commit-scoping discipline** as the rest of dreaming: `git add` only the new/updated `Wiki/` notes and the `Wiki/_meta/` files the audit regenerated. Never `git add -A`.
- If `wiki-ingest` can't run or a source is unreadable, log it and move on — a failed ingest must never abort Phases 1-2.

# Commit & notify

Phases 1, 2, and 3 commit together (or in one Phase-3 follow-up commit if cleaner). **Scope the add to ONLY the files this pass changed** — `git add <explicit paths>` (`memory.md`, any cross-promoted `Wiki/` profiles, `Schema/mistakes.md`, the specific `SKILL.md` files you patched, `Memory/skill-suggestions.md`, `Memory/automation-suggestions.md`). **Never `git add -A`** — the desktop instance shares this working tree. Message: `dreaming: consolidated <dates> (+N auto-applied, +M proposed, +K ingested)`; drop any count that is zero (e.g. only Phase 1 changed → `dreaming: consolidated <dates>`; only ingest ran → `dreaming: ingested <K> source(s)`). The cron diffs your commit and posts a Mission Control note — you don't send it yourself.
