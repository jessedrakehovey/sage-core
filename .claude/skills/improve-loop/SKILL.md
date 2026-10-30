---
name: improve-loop
description: Systematic self-improvement loop for the wedding-videography automation system — audit one subsystem group per pass with a read-only Explore agent, fix the confirmed findings with smallest-viable runtime-tested edits, commit scoped, and periodically run an adversarial fresh-context review of the loop's own commits. Never touches the protected core; routes every the owner-decision to the gated queues. Use when the owner says "improve the system", "finish the system", "systematically go through the system", "improvement loop", or starts a /loop with an improvement prompt.
---

# Improve Loop — audit → fix → verify → self-review

Proven pattern from the 2026-07-01/02 session: 3 subsystem audits, ~30 commits, an
adversarial self-review that caught 3 introduced bugs, zero gating regressions. Run it as
`/loop` ticks (~30 min) or as discrete passes.

## Hard rails (read first, never violate)

- **Protected core is untouchable:** send-gates / approval-gating semantics ("never send /
  never write client-facing anything without the owner's per-item approval"), footage-handling
  safety (never move/rename/delete originals, never format a card before backups verified),
  client-facing contract/pricing/turnaround language, poll tool allowlists
  (`--disallowedTools`), and the dreaming guardrails. Doc edits must preserve gate wording
  verbatim or stronger — diff-check before committing
  (`git diff | grep -i "approv\|never send\|gate\|footage\|backup"` both sides).
- **New scheduled automations are consent-gated:** propose in
  `Memory/automation-suggestions.md`, never register. the owner-decisions (policy, semantics,
  retirements) go to the gated queues (`skill-suggestions ## Pending`,
  automation-suggestions, open-loops) — surface, don't decide.
- **Keep-by-default:** grep for references before removing anything; deletions of
  engine/asset files are proposed, not done.
- **Scoped commits only:** `git add <explicit paths>`, never `-A`. One bad pathspec aborts
  the WHOLE add and a suppressed stderr hides it — keep stderr visible and check
  `git diff --cached --stat` is non-empty before committing.

## The backlog file

`Memory/improvement-backlog.md` is the loop's working memory across ticks and sessions:
ranked punch list with checkboxes, commit hashes on completed items, a `## Log` of ticks,
and explicit "SKIPPED deliberately because…" entries so later passes don't re-litigate.
Read it first every tick; update it every tick.

## Per-tick cadence

1. **Audit (one subsystem group per pass):** spawn a READ-ONLY Explore agent over
   ONE group (e.g. inbound polling / dreaming+stores+shared libs / wiki tooling).
   Ask for a ranked punch list — file:line, what breaks and when, severity, minimal safe
   fix — each item small enough for one tick. Tell it what's already fixed (the backlog)
   and what's protected.
2. **Verify the audit before trusting it.** Audits state plausible-but-wrong findings
   confidently. Test each premise empirically before "fixing" it — check that the file,
   function, or behavior described actually exists and behaves as claimed.
3. **Fix top items, smallest viable edit.** Runtime-test every changed path, not just
   compile/parse: PowerShell must run under REAL PS 5.1 — parse checks pass PS7-only APIs
   that throw at runtime. Python paths get exercised with the real files, and cron-called
   scripts get run once by hand where safe.
4. **Delegate mechanical batches** (doc sweeps, multi-file gating) to Explore agents with
   the rails stated in the prompt; DIFF-REVIEW their work before committing — agents die
   mid-task (salvage + finish) and write plausible-but-broken platform code.
5. **Commit scoped** with a message saying what broke, why the fix is safe, and that gating
   is unchanged.
6. **Update the backlog** (check off with hash, log the tick).

## Every few ticks — the two meta-passes

- **Adversarial self-review:** spawn a fresh-context Explore agent on the loop's own commit
  range ("find bugs the loop INTRODUCED — assume there are some"). Hunt logic inversions,
  failure-mode conflation (disabled vs broken), re-enable paths, encoding/platform traps.
  Fix what it confirms. Treat it as mandatory before declaring an arc done.
- **Live verification:** exercise changed paths end-to-end in production context (run the
  real watchdog/notifier once, watch the next scheduled cron's log). "Compiles" is not
  "works"; "works when I ran it" is not "works from the scheduled task".

## Pitfalls (verify-first pays off)

- **Trace who consumes stale state before "fixing" it.** An audit may propose clearing a
  stale file on upstream failure when the stale re-run is actually an accidental recovery
  path for a previously failed downstream step. Fix the actual hazard (non-atomic write,
  wrong write order: payload first, state second) instead of clearing state.
- **Check the supervisor before choosing an exit code.** "Exit 0 on already-running" is
  correct only if the launcher treats exit 0 as terminal; an unconditional restart-loop
  turns it into spawn churn.
- **Refute-before-fix batting average:** audits confidently assert roughly one wrong
  premise per group. Each wrong premise would have shipped a harmful or useless "fix".
  Grep operational memory — auditors don't know platform behavior.
- **PS 5.1 + `$ErrorActionPreference='Stop'` + `& native 2>&1` = a terminating error on the
  first stderr line.** unittest's progress dots killed the nightly self-test for its whole
  life; a Python warning does the same to any sensor call. Run natives under a local
  `Continue` (see `Invoke-Py` in `scripts/dream_cron.ps1`). And `[int]""` is 0: check
  `^\d+$` before casting a count.
- **"Notify" must end on a surface the owner opens.** Telegram is retired here; anything that
  only spools or logs is invisible. Use `assistant/scripts/notify.py --file` (a Mission
  Control note) or a ticket. Ask of every output: who reads this, and where?
- **Audit agents: `scripts/open_loops.py` writes `Wiki/_meta/open-loops.md` unless
  `--print`.** A stalled agent (stream watchdog) keeps its context: resume it with
  SendMessage and ask for the findings it already has.
- **Keep audit agents SMALL.** Explore agents given a whole subsystem or a multi-commit diff stalled at the
  10-minute stream watchdog every time (2026-10-02, six in a row). Agents told to use only Read/Grep, read at
  most ~6 files and answer in under ~450 words finished in 1-5 minutes with equally good findings. One
  question per agent; run two in parallel instead of one big one.
- **A claim like "no price changed" needs a test of the EDGE cases, not the clean ones.** The clean-lead
  comparison passed while two edge cases (no driving route, state+zip venue) did change numbers; the
  adversarial review caught them. State what was compared, not a blanket claim.
- **Never patch code through a shell heredoc that contains backslashes or backticks.**
  `\b` in a regex arrived as a literal backspace byte and a fenced prompt failed an assert
  (2026-10-01). Write the patch as a `.py` file in the scratchpad (exact old/new strings,
  assert each match, preserve CRLF/BOM) and run that; then
  `grep -c $'\x08' <files>` and run the suites.
- **Transport prompts: never wrap code in markdown fences.** A headless model told to pass
  "this code verbatim" inside ``` fences sends the fences too and the program dies at line 1
  (the CRM connector's executor and snapshot job, seen live 2026-10-01). Use `<code>` tags and say so.
- **A spine job `window` must sit inside the task's firing hours** (06:00-23:30). rescue-kit
  sat at 02-06 and never ran for its whole life; `spine.py --list` showing `last=-` is the tell.
- **Scheduled-task XML is physically UTF-16** — Edit/Write save UTF-8 and silently break
  `schtasks /create /xml` until converted back.

## Wind-down (when audits run dry)

Shift to: committing untracked live code after a secrets scan, growth caps on unbounded
files, wiki lint/orphans, skill-size caps, ingest-backlog pointers — then watch-and-maintain
(verify overnight cycles, refresh surfaces, stay out of files with active parallel work).
End every substantive arc with post-task reflection routed to the gated surfaces.

## Related

- `Memory/improvement-backlog.md` — the live punch list
- [[dreaming]] — the nightly counterpart (this skill is the on-demand, code-focused sibling)
- [[roast]] — use before building anything the loop proposes to build
