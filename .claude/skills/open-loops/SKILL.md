---
name: open-loops
description: Show the owner what's unfinished / what needs to be done, ranked by urgency. Use when the owner asks "what needs to be done", "what's open", "what's on my plate", "what am I forgetting", "open loops", "where did we leave off", or any request for a roll-up of outstanding tasks and priorities across the vault.
---

# Open Loops — on-demand "what needs to be done"

When the owner asks what's unfinished, run the read-only aggregator and relay the result.
Do **not** ask first; this is a read-only roll-up he opted into. It writes the
generated note `Wiki/_meta/open-loops.md` and prints the same report.

## Run it

```
python scripts/open_loops.py
```

(`--print` to print without rewriting the note.)

The script aggregates the vault's existing trackers — it does not own any task
data, it only rolls them up:

| Source | Slice |
|--------|-------|
| `scripts/projects/project_state.json` | per-booking stage, pending deliveries, revision rounds |
| `Wiki/_meta/review-queue.md` | draft wiki notes awaiting the owner's verification |
| `Memory/skill-suggestions.md` (Pending) | proposed behavior changes |
| `Memory/automation-suggestions.md` (Pending) | proposed cron jobs |
| vault-wide grep | `TODO(YYYY-MM-DD)` / `@followup YYYY-MM-DD` markers |
| `assistant/data/cards/live/` | open decision cards waiting for the owner's tap on Mission Control |
| `assistant/data/tickets.json` | the assistant's queue: staged (his move) and new / claimed |
| `assistant/data/exec_tickets.jsonl` | approved actions that did not complete (exceptions) |
| `Memory/improvement-backlog.md` | owner decisions the improvement loop parked for him |
| `scripts/optimizer/.cache/proposals_*.json` | the newest Optimization Scout proposals |
| `scripts/evals/eval_ledger.jsonl` | nightly self-test suites whose latest verdict is FAIL |

## The three tiers (priority model)

Derived, never hand-set. Sort = tier, then oldest-first within tier.

- **Waiting** — someone external is waiting on us, or a job is in flight (has a clock). Couples awaiting delivery, active edit jobs, past-due dated TODOs, unanswered inquiries.
- **Blocked** — waiting on the owner's decision: open cards, staged tickets, skill / automation proposals, owner decisions. No external clock.
- **Backlog** — internal hygiene (wiki drafts to verify, upcoming shoots not yet due, optimizer proposals). No clock.

## How to relay

Lead with the one-line count (`N waiting · M blocked · K backlog`), then the
**Waiting** tier in full (that's the bucket with real-world clocks), then a tight
summary of Blocked and Backlog. Offer to act on the top Waiting item.

## Optional live layer

The script is pure-local (no MCP). When it matters, you can enrich the **Waiting**
tier by pulling live Gmail or Calendar data (via the Gmail and Calendar connectors)
and noting any inquiry or booking event that has **no row** in `project_state.json`
(something arrived that the desk has not yet triaged). Keep this optional — the
local roll-up stands on its own.

## Boundaries

- It changes nothing it reports on: never closes a booking, verifies a note, or accepts a suggestion as a side effect.
  Its one write is the generated note `Wiki/_meta/open-loops.md` (`--print` skips even that; use `--print` when another
  session may be committing wiki files).
- Acting on any item still follows its own gated workflow (e.g. sending a delivery email is draft-only and gated on the owner's approval).
