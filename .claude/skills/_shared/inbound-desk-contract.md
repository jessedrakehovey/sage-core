# Inbound-Desk Contract (shared)

> [!info] What this is
> The single source of truth for the **safety contract** every "watch an inbox →
> triage → draft-only → flag the owner, never send" poll obeys. Future inquiry-poll,
> delivery-desk, or any other inbound desk each point here instead of re-deriving
> the rules (which drifts). This folder has **no `SKILL.md`, so the skill loader
> ignores it** — it is a plain reference doc, not a registered skill.
> Referenced as `[[inbound-desk-contract]]`.

Applies to: [[inquiry-poll]] · [[delivery-desk]] (and any future inbound desk).

## The contract — every poll obeys all seven

> [!danger] 1. DRAFT / PROPOSE ONLY. NEVER send, submit, or post anything.
> Output is **Gmail drafts + a Gmail label + a state-file entry**. the owner reviews
> and executes every send. If any step is uncertain, leave a draft and flag it;
> do not act.

2. **No `--dangerously-skip-permissions`.** The cron runs with an explicit
   `--allowedTools` allowlist (search / read / **draft** / label + read-only browser
   navigate/snapshot/evaluate/wait) **and** a belt-and-suspenders `--disallowedTools`
   denylist (every send/delete tool + `browser_click`/`type`/`press_key`/`file_upload`/
   `fill_form`). The job is then *structurally* incapable of sending or clicking a
   Submit button.

3. **Idempotent.** Dedupe every item against a **git-ignored state ledger**
   (`*_state.json`) keyed by Gmail message **or thread** id. A re-run (including a
   retry) must never double-act. This is what makes retry safe.

4. **Resilient.** Invoke via `scripts/lib/run_claude.ps1` (`Invoke-ClaudeHeadless`)
   so transient socket/5xx errors retry instead of silently failing.

5. **Notify, don't act.** The signal to the owner is a Gmail **label** + a digest line
   in the log. Urgent items (e.g. same-week inquiry, day-of timeline change) get a
   louder, separate label.

6. **Client privacy.** Deterministic identity lookups only — never *infer* a couple
   from partial information. Never put personal or contact details (addresses, phone
   numbers, financial details) into a git-tracked note or draft beyond what is
   operationally needed for the shoot.

7. **No git writes.** The state ledger is git-ignored and nothing else is committed —
   this avoids the multi-writer git hazard with the desktop ("Cowork Agent") instance.
   (Headless auth note: a nested `claude -p` shows "Not logged in" inside a session
   because the harness scrubs the token — a false negative; the real Scheduled Task
   run authenticates fine.)

## Standard output per new item

1. One or more **Gmail drafts** (`mcp__example-mail-films__draft_email` or `mcp__example-mail-personal__draft_email`, `mimeType:"text/html"`; never a `mcp__gmail__*` tool - that server is the property-management account),
   each ending with the **sign-off block** below for any client-facing mail.
2. A **label** on the source email (`get_or_create_label` → `modify_email`).
3. A **state-ledger entry** keyed by message/thread id.
4. A one-line **log summary** so the cron log shows what happened.
5. If nothing new: change nothing and reply with the desk's exact no-op sentinel.

## Sign-off block (every outbound client-facing email ends with this)

Embedded here verbatim so a headless cron always has it without depending on recalled
memory. `TODO` — confirm the owner's preferred sign-off name, business name, contact info,
and any disclaimer text once the brand is finalized.

```
TODO: confirm sign-off block with the owner.
Suggested shape:

<Name> | <Title>
<Business Name>
<City, State>
<Website / phone if shared with clients>

[Any legal disclaimer the owner wants on client emails]
```
