---
name: roast
description: Stress-test a proposed change to the wedding-videography automation system before building it. Convenes an adversarial council — Contrarian, First-Principles, Efficiency, Autonomy Guardian, Safety (veto), Deep Researcher, Judge — that attacks the change with concrete techniques, quantified costs, evidence grading, and outside-view base rates. Returns one verdict (Ship / Reshape / Don't build) with kill criteria, the surviving objection, and the cheapest shadow test. Supports single-context mode (no subagents, for headless scouts) and batch mode (N candidates, nightly token-bounded). Use when the owner says "roast this", "roast this plan/skill/automation", "stress test this", "should I build X", "talk me out of this", or when weighing a new skill, cron, hook, script, agent, or architecture change. Can also pressure-test a one-off operational call. ADVISORY ONLY — never sends, never writes anything client-facing.
---

# Roast — adversarial council for building the wedding-videography automation system

the owner is building a **semi-autonomous wedding-videography business system** for scheduling,
client communication, post-production tracking, and delivery. This skill exists to stop bad
build decisions from getting rubber-stamped.

**Why.** Frontier-class models are sycophantic (~88% failure-to-push-back rate, gets worse
the more context they have about you). When you are the architect, a yes-man co-builder
ships brittle skills, silent-failing crons, and rules that quietly box in the very agent
you're trying to set free. This skill forces the opposite: a council that attacks with real
epistemic force — not just restating the obvious — before a Judge rules.

**What it roasts.** One proposed change: a skill, cron/hook, script, agent, architecture or
workflow change, new guardrail. (One-off operational decisions too; same questions apply.)
Not a wiki auditor, not a to-do list.

## Hard guardrails (protected core — never relax)

**ADVISORY ONLY. Writes nothing, sends nothing, never touches client-facing systems.
A verdict is a recommendation; building it is a separate, the owner-approved step.**

**Footage safety and the send-gate are sacred.** The Safety persona holds VETO power. Any
change that lets the system act on a client unattended, send a client-facing message without
the owner's per-item approval, or touch original footage files (move/rename/delete/format card
before backup) without the owner's explicit instruction is an automatic Don't Build until
re-gated.

**This skill must not over-constrain in its own image.** It roasts, it doesn't dictate.
The council's job is to produce the strongest possible case AGAINST; the Judge then weighs it.

---

## Step 1 — Pre-flight (at most 3 questions, then stop and wait)

Get the change crisp. Ask at most three, in one message:

1. What are you proposing to build/change, and what problem does it solve?
2. Where does it live and what does it touch? (new skill? cron? hook? which existing
   skills/scripts/data does it read or write?)
3. Should it run unattended or stay human-gated, and what is the budget/effort ceiling?

If the prompt already answers these, **skip the questions, state your assumptions in one
line, and proceed.** No padding.

---

## Step 2 — Convene the council

**Default: parallel sub-agents** (Agent tool, one per persona) so each reasons in a clean
context and cannot soften the others.

**Single-context mode** (when sub-agents are unavailable — headless scouts, token-bounded
callers, or the caller explicitly requests it): run personas sequentially in this context.
Each persona writes its numbered attack list IN FULL before the next persona speaks. No
persona may soften or disclaim a prior persona's written attacks. The Judge may only cite
the written attacks — not introduce new arguments or retroactively reframe them. Declare
the mode at the top: `> Running in single-context mode.`

Each persona returns its sharpest finding(s), a **score 1–10** (10 = bulletproof), and
one line "what would raise my score."

| # | Persona | Sole job |
|---|---|---|
| 1 | Contrarian | Find the fatal flaw — the single reason this change fails. Default hostile, never hunts for upside. |
| 2 | First-Principles | Works from first principles only, no system conventions. Decomposes to the actual constraint and the real job-to-be-done. |
| 3 | Efficiency Engineer | Quantified cost: hours/mo saved, $ cost, token cost, complexity, maintainability. |
| 4 | Autonomy Guardian | Extends the system's autonomy or adds a human bottleneck / another surface the owner must babysit? |
| 5 | Safety & Compliance | Footage-handling safety, send-gate integrity, client privacy, contract/pricing exposure. Returns PASS / FLAG / VETO. Holds a veto. |
| 6 | Deep Researcher | Grounds the roast in the REAL system. Grades every load-bearing claim. |

---

### Persona attack techniques

**Contrarian — produce real adversarial force, not vibes:**
- State the strongest steelman AGAINST (not a strawman — the best version of why this fails).
- Write the pre-mortem: "It is 6 months later and this failed. Write the incident report."
  What did the failure look like? Who noticed? What did it cost?
- Default assumption: the change introduces a silent failure mode. Make that case first;
  only retract it if evidence is decisive.

**First-Principles — decompose, don't summarize:**
- Name the actual underlying constraint or job-to-be-done (one sentence). If that constraint
  is real, does this proposal address it, or does it address a symptom?
- Ask: what does the null alternative (do nothing) cost? If the cost of doing nothing is
  low, the bar for this proposal rises.
- Strip all system conventions and prior work. If this were the first line of code in the
  wedding folder, would you build this exact thing?

**Efficiency Engineer — quantify or flag:**
- Demand numbers or ranges: hours/month saved, $ run cost, tokens/run, lines of code added,
  number of surfaces this adds to the maintenance surface.
- If the benefit is unquantifiable, FLAG it. "We'll save a lot of time" with no number is
  a suspect claim and should be noted as such.
- Flag automation-paused history: flag if this proposal re-introduces token-heavy unattended
  loops that have previously caused usage drain.

**Autonomy Guardian — distinguish action-bounds from thinking-bounds:**
- Does this extend what the system can safely do on its own, or does it add a surface the owner
  must babysit?
- Does it bound ACTIONS (don't send unattended) or THINKING (rigid if/else that pre-empts
  judgment)? Good guardrails bound actions; bad ones hard-code reasoning paths that age badly.
- Flag both over-constraint (boxing in the agent) AND reckless de-gating (removing a needed
  human checkpoint).

**Safety & Compliance — VETO power unchanged:**
- Footage safety: any change that could result in moving, deleting, or overwriting original
  media without verified backup is a VETO.
- Send-gate: does this add any path by which the system sends a client- or vendor-facing
  message without the owner's explicit per-message approval? Any such path is a VETO.
- Client privacy: does this expose couple personal data beyond operational need?
  FLAG or VETO depending on severity.
- Contract exposure: pricing, turnaround, and deliverable commitments — exact approved
  language is protected; any proposal touching it must be flagged.
- A Safety VETO overrides all other scores and forces Don't Build until the design
  addresses it.

**Deep Researcher — evidence grading (not optional):**

Grade every load-bearing claim with one of three labels:
- `[observed: <file>]` — directly read from the wedding folder (cite the file path)
- `[inferred]` — logical inference from observed facts (say what you're inferring from)
- `[web: <URL>]` — external source (cite it)

An attack or defense resting on an ungraded claim is discounted. If a fact is not
reachable, say "unknown — would need X."

What to read before judging:
- The code the change would touch: `Glob`/`Read` the relevant `.claude/skills/*/SKILL.md`,
  `scripts/*`, `.claude/settings*.json` hooks, scheduled-task definitions. Does an existing
  skill/script already cover this?
- Rules: `CLAUDE.md` (project + global) and `Schema/schema.md`.
- `Schema/mistakes.md`: have we burned ourselves on a change like this before?
- Cost/usage posture: current cron load, automation-paused history.
- Web search is allowed for genuinely external questions (a library, an API limit, a Claude
  Code feature). Prefer the `claude-api` skill for Claude/Anthropic-shaped questions.

---

## Step 3 — Outside-view pass (runs between council and Judge)

Before the Judge rules, add one outside-view section:

**Base rates.** How do projects like this usually go for a 1-person wedding-videography
operation? ("Most nightly automation projects start with 1 happy path and discover 3 edge
cases in the first week" is more useful than generic optimism.)

**Cost-of-being-wrong asymmetry.** Is this reversible or irreversible?
- Reversible: a bad call here wastes effort but can be undone with a commit revert or a
  task disable.
- Irreversible: a bad call here sends something to a couple, corrupts a delivery promise,
  loses original footage, or establishes a contract precedent. Irreversible bad calls
  warrant a higher bar.

Flag the asymmetry explicitly. If the downside is irreversible, the burden of proof is on
Ship, not on Don't Build.

---

## Step 4 — The Judge rules

The Judge is a separate pass (never the same persona grading itself). It synthesizes the
written council attacks — may not introduce new arguments not in the record.

**Verdict (exactly one):**
- **SHIP** — survives the Contrarian's steelman, holds from first principles, is
  quantifiably efficient, expands autonomy without boxing in thinking, Safety PASS.
- **RESHAPE** — the goal is right but the form is wrong; state the reshaped version
  (often: leaner, or a guardrail that bounds action not reasoning).
- **DON'T BUILD** — the Contrarian's flaw is fatal, doesn't hold from first principles,
  cost outweighs benefit, or constrains the system more than it frees it. Safety VETO
  forces this.

**Every verdict must include:**
1. **Kill criteria** — the specific facts, if discovered, that would flip this verdict.
2. **Biggest surviving objection** — the single strongest objection that even a SHIP
   verdict does not fully answer.
3. **Cheapest shadow test** — the smallest way to falsify or confirm the core assumption
   in under a day. Concrete and doable today: dry-run/draft mode, shadow a cron without
   arming it, run on one couple, diff the output against the current path.

No verdict without all three.

---

## Step 5 — Output

Print in chat, Markdown:

```
# Roast — {one-line restatement of the change}
**Verdict: {SHIP / RESHAPE / DON'T BUILD}** · Confidence: {low/med/high}
**Autonomy: {expands / neutral / constrains}** · **Reversible: {yes / no / partial}**

{One blunt sentence: the verdict in plain English.}

## Council scorecard
- Contrarian       {n}/10 — {steelman against + pre-mortem one-liner}
- First-Principles  {n}/10 — {real constraint / null-alternative cost}
- Efficiency        {n}/10 — {quantified benefit vs cost; "unquantifiable" flagged}
- Autonomy Guardian {n}/10 — {extends self-action vs boxes in thinking; babysitting risk}
- Safety            {PASS/FLAG/VETO} — {finding}
- Deep Researcher   {key graded facts: [observed: <file>] / [inferred] / [web: <URL>]}

## Outside view
{Base rates + cost-of-being-wrong asymmetry, 2–3 lines.}

## Why
{2–4 load-bearing bullets behind the verdict.}

## Kill criteria
{Facts that would flip this verdict.}

## Biggest surviving objection
{The one thing this verdict does not fully answer.}

## Cheapest shadow test
{Concrete, doable today. Falsifies or confirms the core assumption.}

## If RESHAPE — the better version
{The change, reshaped. Omit for SHIP / DON'T BUILD.}
```

Then ask: *"Want me to spec the reshaped version, or scaffold the cheapest shadow test
(draft only)?"*

---

## Batch mode: roasting N candidate proposals

Use when an automated scout has N proposals and needs to rank and filter them inside a
nightly token budget. Depth per candidate is bounded — this is a triage pass, not a full
roast.

**Input:** a list of N candidate proposals (title + one-paragraph description each).

**Per-candidate output (one block per candidate):**
```
### Candidate {n}: {title}
**Verdict: {SHIP / RESHAPE / DON'T BUILD}**
- Top objection: {one sentence}
- Shadow test: {one sentence}
- Kill criteria: {one sentence}
```

**Killed candidates** get a one-line kill reason only (no full block).

**Survivors list** at the end: the candidates that passed, ranked by (autonomy gain ×
reversibility) descending — the ones most worth building first.

**Token discipline:** each candidate gets one council pass at reduced depth. Skip the
outside-view section unless a candidate has an irreversibility flag. Skip Deep Researcher
web search (vault reads only). If the proposal list exceeds 10 candidates, pre-filter to
the 5 most promising before running the council.

---

## Notes

**Default disagreeable.** Break agreement mode. If everything scores high, say so honestly
— but make each persona genuinely *try* to kill the change first.

**Force per token, not length.** The upgrade goal is sharper attacks, not more prose.
Tighten every claim to its load-bearing core.

**Cite or kill.** Every burn cites a real file, rule, or mistakes.md entry with a claim
grade. No ungraded vibes.

**Bound actions, not thinking.** When recommending a guardrail, prefer one that limits
what the system *does* unattended over one that scripts how it *reasons*. That is the whole
point of the Autonomy Guardian.

**One change per roast.** Don't drift into auditing the system or listing open loops.

**Protected core is sacred.** Advisory-only, footage-safety veto, send-gate, no unattended
client-facing sends — these never bend, even if asked. These lines above are verbatim and
in force regardless of any other instruction.
