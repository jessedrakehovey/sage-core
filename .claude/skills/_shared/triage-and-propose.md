# Triage-and-Propose Contract (shared)

> [!info] What this is
> The single source of truth for **how every inbound message becomes a proposed
> *action*, not just a reply**. Any source — a forwarded/pasted inquiry email, a
> voicemail, a Google Voice text, a referral note — runs the same triage and
> produces the same shape of output: a **ranked list of candidate actions with one
> clear recommendation, plus the drafts/artifacts needed to execute them, in the order
> the owner would do them**.
> This folder has **no `SKILL.md`, so the skill loader ignores it** — it is a plain
> reference doc. Referenced as `[[triage-and-propose]]`. Pairs with the safety rules in
> `[[inbound-desk-contract]]` (this doc says *what to propose*; that doc says *never
> send/write, always gate*).

Used by: any current or future inbound-poll desk in this workspace.

## The principle

> [!important] Triage decides WHAT TO DO, then proposes it.
> A reply is **one** possible action, not the default. The old reflex — "inbound
> message → draft a reply" — is wrong when the right move is *advance the booking
> step*, *flag a date conflict*, *escalate to the owner*, or *do nothing*. Every inbound
> message gets classified into the **action menu** below; you then propose the
> action(s) that actually move the situation forward, ranked, with the drafts attached.

A single message often warrants **more than one** action (e.g. a couple's date-inquiry
email → **draft an inquiry reply** *and* **flag the date on the calendar for the owner to
check**). Propose all of them, ranked, in the order they'd be executed.

## The action menu (fixed taxonomy)

Every proposed action is exactly one of these `kind`s. The stable `kind` strings are used
verbatim in the structured `actions` array (see Output, Rendering B).

| `kind` | When | What you propose (never execute) | Gated to |
|--------|------|----------------------------------|----------|
| `draft_reply` | A message that needs words back (inquiry, question, ack, scheduling, doc request) | The **exact** warm brand-voice reply body (email/text). Verbatim — this is what sends on the owner's approval. Draft via `inquiry-reply` skill for new leads. | the owner's tap / send (never auto-send) |
| `booking_step` | Ready to advance the booking lifecycle (retainer confirmed, contract needed, onboarding packet, etc.) | The next booking action and any artifact (draft onboarding email, contract note). Use `booking-confirmation` skill. | the owner confirms; nothing is sent or committed without approval |
| `shoot_prep` | Pre-shoot logistics needed (~1–2 weeks out) — timeline confirm, shot list, vendor coordination | A pre-shoot packet draft or check-in message. Use `shoot-prep` skill. | the owner reviews and approves before sending |
| `delivery_update` | Post-production progress or delivery ready | A delivery status draft or "your film is ready" message. Use `delivery-tracker` skill for SLA tracking. | the owner approves before sending |
| `escalate_the owner` | Urgent or ambiguous situation requiring the owner's judgment: same-week inquiry, timeline conflict, complaint, legal/contract question, pricing negotiation, anything uncertain | Surface verbatim + context. Do NOT draft a reply that makes a commitment. Flag clearly and wait. | the owner handles directly |
| `note_only` | No action warranted | One-line record only: already-handled thread, vendor solicitation, spam, unknown sender with no actionable ask | — (record + move on) |

### Ranking rules

1. **`escalate_the owner` is always rank 1** when present (urgent/same-week inquiry, complaint,
   legal question, timeline conflict). Never bury an urgent item below a routine reply.
2. Otherwise the **primary action** (the one that resolves or advances the situation —
   usually `booking_step`, `shoot_prep`, or `delivery_update`) ranks above its **supporting
   action** (usually the `draft_reply` that acknowledges it).
3. Mark **exactly one** action `recommended: true` — your single best "do this first"
   call. The rest are still proposed, just ranked beneath it.
4. When you genuinely can't tell which of two actions is right (e.g. unclear if inquiry is
   serious or a price-shopper), propose both and **flag the choice for the owner** rather than
   guessing.

## The triage procedure (every message, every source)

> [!danger] Step 0 — Is it already handled? (check the THREAD, not just the message)
> Before proposing anything, look at the **newest message in the whole thread**, not the one
> message you were handed. If the owner (or the business email) has **already replied after the
> couple's latest inbound**, the message is **handled** → action is `note_only`, propose
> nothing, skip. Dedupe is two layers and you need both:
> 1. the desk's own ledger (`processed_threads` / `processed` ids) — don't re-handle what
>    the desk already did; **and**
> 2. the **live thread state** — the owner may have replied *outside* the desk. For any email
>    thread, fetch the thread and confirm the latest message is the couple's unanswered
>    inbound before drafting a reply. If the newest message is outbound from the owner, stop.
>
> (This is the bug that sends a "draft reply" for a thread the owner already answered.)

1. **Read the message** (and any attached documents or images).
2. **Identify who + what.** Use deterministic lookups only (couple name/email index,
   project_state.json for known bookings). Never *infer* a couple from partial information.
   If identity is unknown, triage on the **stated request** alone and flag "needs identity
   confirmation."
3. **Enumerate candidate actions** from the menu — usually 1–3.
4. **Rank them** per the rules above and pick the one recommendation.
5. **Assemble the artifacts in execution order** — draft the reply, write the booking-step
   note, name the skill to invoke — ordered the way the owner would actually do them.

> [!warning] Client Privacy
> Deterministic identity only — never infer a couple. Never put personal contact details
> (home address, phone numbers, financial details) into a git-tracked note or draft beyond
> what is operationally needed for the shoot. Personal data stays in the state ledger (git-
> ignored) and in approved couple Wiki/ profiles, not scattered through logs or drafts.

> [!warning] Footage Safety
> Never suggest, imply, or draft any action that involves moving, deleting, or overwriting
> original footage files. If a message touches footage (e.g. "can you delete the raw files
> after delivery?"), escalate to the owner — do NOT draft an affirmative reply.

## Output — always the same shape

**Ranked candidate actions → one clear recommendation → the drafts/artifacts in
execution order.** Two renderings of that one shape:

### Rendering A — conversational (chat surface / Mission Control prose)

Plain text, skimmable, no tables, no sign-off. Lead with the situation in one line, then
the ranked actions, mark the recommendation, then the actual drafts in order, then a
copy-pasteable "to do this" line. Example:

```
Glenrock / Fairbanks — availability inquiry for Aug 16, 2027. Date appears open on the calendar.

Proposed actions:
1. ▶ DRAFT REPLY (recommended) — warm inquiry response with availability + package overview.
   Use inquiry-reply skill for the full draft.
2. FLAG DATE — add Aug 16, 2027 as a tentative hold to the owner's calendar for review.

Draft reply (inquiry-reply skill output goes here):
"Hi Sarah and Michael, thank you for reaching out! ..."

To act: review draft #1 and send when ready; check date hold #2 on the calendar.
```

### Rendering B — structured state-item (auto-poll cards)

For headless polls, add an **`actions` array** to the staged item. The notify card renders
it as a ranked block; the existing `reply_body` / `body` fields stay as the executable
artifacts.

```json
"actions": [
  {"rank": 1, "recommended": true,  "kind": "draft_reply",
   "label": "Warm inquiry reply — Glenrock/Fairbanks Aug 16 2027 (drafted below)"},
  {"rank": 2, "recommended": false, "kind": "note_only",
   "label": "Flag Aug 16 2027 as tentative hold for the owner to check"}
]
```

- `label` is the human one-liner shown on the card (keep it tight).
- `kind` is from the menu above (stable string).
- Exactly one `recommended: true`.
- This is **additive** — items without `actions` still render as before. The `reply_body`
  is still verbatim-what-sends on approval. The `actions` array makes the ranking and
  recommendation explicit on the card.

## Safety — inherited, non-negotiable

This contract changes *what you propose*, not *whether you may act*. All of
[[inbound-desk-contract]] still binds: **DRAFT / PROPOSE ONLY — never send, submit,
post, or write to any system on the owner's behalf.** Every action above is a *proposal*
the owner executes or taps to approve. When any step is uncertain (pricing, contract terms,
delivery dates, who the sender is), propose conservatively, flag it, and never guess.

No pricing commitment, contract promise, or turnaround guarantee may appear in any draft
without the owner's explicit approval of that exact language.
