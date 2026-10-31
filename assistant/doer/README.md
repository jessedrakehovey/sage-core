# The doer - Sage's hands (assistant/doer)

Built 2026-10-05. Sage (`assistant/agent`) is a read-only brain. When an owner asks for
something that must be *done* in an account or on a website, Sage hands it here. The doer is
not a standing lane: nothing loops, nothing polls. One headless model turn starts for one
task, drives one shared browser, and exits. Zero tokens when nothing waits.

## How a request flows

```
"Sage, update my DMV address"  ->  Sage replies "On it" + SUGGEST {"do": {...}}
          actions._queue_do  ->  ticket kind agent_run  +  doer.wake()  (starts within seconds)
                                                     |
                 doer.py --tick: claims ONE agent_run ticket, opens the browser (browser.py),
                 runs one `claude -p` turn with prompt.md + the private playwright-doer MCP
                                                     |
        tier A/B: does it, DOER_DONE  ->  ticket done, result posted in the owner's chat
        tier C:  fills everything, screenshots the review page, report.py checkpoint
                 -> card on Mission Control (step + every value + screenshot) -> DOER_CHECKPOINT
                      owner taps Approve  ->  exec ticket agent_run (sha-bound)
                      drain.py  ->  doer.py --approved  ->  resumes the same model session,
                                    performs exactly that step  ->  DOER_DONE  ->  chat
        login page: `login_fill.py --url ...` types the vault login; the model never sees it
```

The spine job `doer-tick` (every 10 min) is the backstop when the wake did not fire.

**Live status (2026-10-07).** `assistant/out/doer/status.md` says what the doer is doing right now
or where its last task stopped: the task, the phase (written by doer.py: working, waiting for a tap
on card X, finished, blocked) and a `Now:` line the model keeps current (what it sees or waits on,
e.g. a login code). Sage reads it to answer "where is it?"; `doer.py --status` shows it too. A
checkpoint is relayed into the requester's Chat DM as well as the card; a code card says to type the
code in the note box on Mission Control (Chat buttons cannot carry a typed code). Since 2026-10-25 an owner
can also just type the code in chat: a bare 4-10 digit line answers the newest open code card by code
(`bridge._code_for_card`, no model turn; the code is kept only in the ticket, never in the transcript).

**Hand-off checkpoints (2026-10-28).** When the last step is the owner's own (typing the payment at a
checkout, signing), the turn sets `"handoff": true`: before the card posts, `browser.show_on_screen()` moves the
running browser onto the main monitor, maximized and in front (no restart, so the cart survives) and reads the
window back. The card and the chat line say "open on your screen" only when that read-back is visible; the card
has no Approve (just "I finished it" / Cancel), and the browser is left open. `browser.py --front` does the same by hand.

**No code came (2026-10-07).** A code card also lists the site's other sign-in ways as buttons
(`Use instead: Email a code to j***@gmail.com`, ...) plus `No code came`. Tapping one needs no code:
the next `doer-tick` (within 10 min) resumes the task's own session on that way, or on one the doer
picks (authenticator app, then email code, then another device; never the same text again).

**Whole reports (2026-10-23).** A `DOER_DONE` / `DOER_ANSWER` that runs over several lines (a list of
options, where things sit) reaches chat and the ticket note whole, up to 1500 characters
(`doer.REPORT_MAX`), instead of stopping at the first line break.

## Files

| File | Role |
|---|---|
| `doer.py` | the runner: tick / run / approved; allow + deny lists (gate target `doer`); result parsing; reply into chat |
| `prompt.md` | what the model turn is told: the browser, the three tiers, the checkpoint protocol, the final-line markers |
| `policy.json` | tier C hosts and actions, the `never` list, time limit |
| `browser.py` | the one shared Chromium: `~/.claude/pw-wv-cli-profile` with a CDP port, off-screen; `--show` puts it on screen for an owner to sign in |
| `login_fill.py` | signs in from the vault into the page the model is on; prints SIGNED_IN / NEEDS_CODE / NO_ENTRY / FAILED, never a secret |
| `vault.py` | the agent's KeePass file (`assistant/data/doer/agent.kdbx`, key in Credential Manager `kdbx:agent`); import from Google Password Manager, add, list, has, remove |
| `report.py` | the checkpoint card (the only way the turn talks to an owner mid-task) |
| `assistant/data/doer/` | git-ignored: the vault, sessions (ticket -> model session), budget, locks |
| `assistant/out/doer/<ticket>/` | git-ignored: screenshots, the MCP config, result JSON; Mission Control serves the PNGs at `/api/doer/shot/<ticket>/<name>` |

## The three tiers (prompt.md, policy.json)

- **A** read or trivially reversible: just do it.
- **B** a reversible write that notifies nobody: do it, report with a screenshot.
- **C** irreversible, outward, money, identity, legal, or on a government / bank / payment /
  registrar site or in a Google/Apple/Microsoft account: fill everything, stop at a checkpoint
  card. The owner's tap is the only way the last step happens.
- **Never**: change or reset a password, turn off two-factor, delete or close an account, trade
  securities or crypto, touch the property-management business. A captcha or a bot wall ends
  the attempt; the owner finishes by hand in `browser.py --show`.

What is mechanical and what is instruction: the allow/deny lists, the private MCP, the Bash
limits, the vault (the model cannot read it), the sha-bound approved step and the EXEC_VERBATIM
rule on cards are enforced by code. The tier decision itself is the model following prompt.md;
a wrong tier call means a tier C step without a card. That is why tier C is drawn wide and
"when in doubt it is C", and why every run is logged (`~/.claude/doer-wv.log`, `out/doer/`).

## Logins

1. **Pre-signed-in sessions.** Sign in to a site once in the agent's browser
   (`python assistant/doer/browser.py --show`), close the window. Cookies persist about 30 days.
   Most sites need nothing more.
2. **The vault**, for sites that log you out or want a fresh login each time:
   ```
   python assistant/doer/vault.py init
   python assistant/doer/vault.py import-chrome "C:\Users\the owner\Downloads\Chrome Passwords.csv"   # passwords.google.com -> Settings -> Export; delete the CSV after
   python assistant/doer/vault.py add "CA DMV" --url https://www.dmv.ca.gov/portal/ --username the owner@... [--totp]
   python assistant/doer/vault.py list
   ```
   Only the logins you put here are reachable. TOTP secrets make two-factor automatic; an SMS or
   email code becomes a card ("type the code in the note box, tap Approve"). To open the file in
   KeePassXC: `vault.py key`.

## Staying signed in: clean shutdown (2026-10-13)

Why sites kept "signing out" and the browser "never finished starting": every close was a hard
kill (the polite quit went through Playwright, which is missing from the system Python and hangs on
many tabs). Chromium took each kill as a crash, so it restored every old tab on the next start (60 by
10/12; the MCP timed out attaching to them) and lost any cookie not yet flushed to disk, which is
often a fresh sign-in. Now `browser.py`:

- closes leftover tabs over plain HTTP on every open and close (`--prune` by hand);
- quits with `Browser.close` on the raw CDP websocket (stdlib only), waits up to 20 s, and uses
  `taskkill` only as a last resort. The profile's `exit_type` should read `Normal`;
- sets "continue where you left off" before launch, so session-only sign-in cookies survive.

Warm list (daily `doer-warm`): typeform, lensrentals, musicbed, weddingpro. Vimeo is off because
its login wants a human. Do not warm sites that text a code (Facebook, Netflix, Coinbase, ...).

## Sign-in playbooks and the warm pass (2026-10-06)

"A script for every sign-in we use", learned rather than written: the first time `login_fill.py`
signs in to a host it saves what worked (login URL, the username / password / code selectors, one
step or two) to `assistant/data/doer/sites.json`, and uses that playbook first next time. A
playbook can be written by hand too (`user_sel`, `pass_sel`, `otp_sel`, `submit_sel`, `login_url`,
`two_step`, `domain_wide`; add `_fixed` to a key to stop learning from overwriting it).

```
python assistant/doer/login_fill.py --test https://www.dmv.ca.gov/portal/    # sign in now, on screen, save the playbook
python assistant/doer/login_fill.py --sites                                  # what is known, last result per host
python assistant/doer/login_fill.py --warm [--all] [--only host] [--show]   # sign in to every playbook with warm=true
```

Set `"warm": true` on the sites you ask about often. The Day Spine job `doer-warm` runs the warm
pass daily (06:00-08:00, off-screen) so those sessions never go cold; its report is
`sites_status.json` (connection `doer-sessions`): a captcha, a code request or a wrong password
shows up there with the site named, and nothing is retried on its own.

## Run it

```bash
python assistant/doer/doer.py --status
```

```bash
python assistant/doer/doer.py --tick --dry-run
```

```bash
python assistant/tests/test_doer.py
```

Config: `assistant/config.json -> doer` (model, turn limits, runs per day, profile, CDP port,
`offscreen`). After changing the tool lists in `doer.py`, re-freeze the gate baseline
(`python scripts/guardrails/check_gates.py --freeze`) and commit both.

## The computer lane: hands on the PC (2026-10-11)

the owner: "fully functional Jarvis on the go, full control of computer and connections, safe, self
skill building". The browser is one place work gets done; the machine itself is the other.
`assistant/doer/computer.py` runs a `do` whose `where` is `computer` (Sage sets it when the ask is
about drives, files, footage checks, ffmpeg, a vault script, a measurement, a connection, or a
script to write): same ticket kind (`agent_run`), same `doer-tick`, same cards, same reply path.
`doer.py` dispatches on `payload.where`; `--approved` does too.

**Why a shell is safe here: the tier is decided by code, not by the model.**
`computer_policy.py` runs as a PreToolUse hook on every Bash / Write / Edit call of the headless
turn (passed with `--settings`, outside the model's reach; fail-closed on anything it cannot parse):

| Tier | What | Hook says |
|---|---|---|
| A | read, list, probe, hash, ffprobe, git status, a read-only vault script | runs |
| B | a write inside `write_roots` (`assistant/out/doer/<ticket>/`, `assistant/doer/tools/`, Temp), a copy, a folder, a tool that passed the scan | runs; reported |
| C | a delete or move anywhere, a write outside the roots, install, kill a process, any network send, `git commit`/`push`, a service or setting | refused: "post a checkpoint"; the card carries the **exact command**; Approve re-runs that one command once (sha-bound, `DOER_APPROVED_SHA` + a used-marker) |
| NEVER | format/wipe, delete/move/overwrite **footage** (media extensions, `Video/`, `Audio/`, `Card N/`, `DCIM`), credentials and password stores, the approval chain, the registry, scheduled tasks, firewall/security, encoded/obfuscated commands, `eval`/`exec`, a nested `claude`, the property business, destructive git | refused for good; no card can unlock it |

Scripts are scanned before they run (`python x.py`, `python -c`, `powershell -Command` text alike):
forbidden imports (`socket`, `smtplib`, `ctypes`, `winreg`, `pyautogui`, `keyring`, ...) are NEVER,
deletes/moves/sends inside a script make the run tier C, a script outside the tool folders is C
even when clean. Policy data: `computer_policy.json` (protected core: the builder may not touch
it). No MCP servers (`--strict-mcp-config`, empty), no PowerShell tool, no web, no nested agents;
gate target `doer-computer` in `check_gates.py`.

**Self-built tools.** A job that will come back is saved as `assistant/doer/tools/<slug>.py` with a
header (`# tool / # triggers / # tier / # usage / # needs`); the scan runs on Write (a tool with a
socket is not even saved) and again on every run. `computer.py --index` rebuilds `tools.json`
(zero-LLM; also after every run); the next turn's prompt lists the tools, matches starred; the
report names the tool and Sage writes a `skill` from it. Deletes go behind `--apply` so the dry run
stays tier B. Tools are git-tracked: read or `git revert` any of them.

```bash
python assistant/doer/computer.py --status
```

```bash
python assistant/doer/computer_policy.py classify "rm -rf D:/CacheClip"
```

```bash
python assistant/tests/test_computer.py
```

Logs: `~/.claude/doer-computer-wv.log` (every hook decision: ticket, tool, tier, allow/deny, the
command). Config: `assistant/config.json -> doer.computer` (`model`, `max_turns`,
`turn_timeout_s`, `approved_timeout_s`; falls back to `doer.*`). Not yet proven live: the first
model run (the nested CLI cannot authenticate inside a session); the hook, the sha-bound approval,
the scan, the cards and the dispatch are tested end to end without the model.

## Evidence, and the jobs that come back (2026-10-15)

the owner: "fix Sage so it can do the last few things I requested and when it has evidence of
completion report back", after Corwin's fixed SFX track could not be pulled from Gmail into the
the owner Beach timeline (the browser doer had no download right, the computer lane had no tool, and
Resolve was closed). Three things changed:

- **Evidence is checked by code.** A computer-lane turn writes `<out_dir>/evidence.json`
  (`files` with path/bytes/sha256, `checks` with ok/detail, `notes`). `computer.verify_evidence`
  re-stats and re-hashes every file row and `evidence_suffix` puts the result under the DOER_DONE
  line in chat ("Checked: ..." / "Checked, and something is off: MISSING ..."); a DONE with no
  evidence file is relayed as an unchecked claim. The ticket's done-note carries the tally.
- **Two vault tools the lane runs as written** (listed in `computer_prompt.md`, both append their
  own evidence rows when `DOER_COMPUTER_TICKET` is set): `assistant/scripts/mail_attach.py`
  (Gmail attachments and the Google Drive links Gmail puts in a body; sha256 + Drive md5 checked,
  never overwrites, refuses camera-media folders; default home `~/Videos/Sage Inbox`, a new write
  root) and `editor/resolve_swap_audio.py` (new file on a new track at the old clip's position,
  old clip muted, project saved, `--launch` opens Resolve when closed; `--list`, dry run by default).
  The policy now scans a script run through a full interpreter path (`.../Python312/python.exe
  editor/x.py`) and treats `editor/` like `scripts/` (scanned, not tier C).
- **The learn bug.** `import learn` in `doer.py` / `computer.py` resolved to
  `assistant/scripts/learn.py` (the corrections ledger) whenever that was already loaded, so every
  run since 10-12 logged `learn failed: AttributeError` and recorded nothing. `doer._learn()` loads
  the doer's own file by path (`sys.modules["doer_learn"]`).

Tests: `assistant/tests/test_mail_attach.py` (+ `test_crm_exec.py` for the CRM connector's message kind).

## The self-learning engine (learn.py, 2026-10-11)

the owner: "how fast will it learn new skills - make the self learning engine", then "have it self
pace based on lessons and improvement opportunities". Before: learning was optional and one-shot
(a tool kept only if the model saved it, a skill only if Sage wrote one from the report, nothing
measured, nothing repaired). Now, zero-LLM, the moment a run ends (`learn.after_run`, called by
both lanes):

| Step | What happens | Speed |
|---|---|---|
| **record** | every run -> `assistant/data/learn/runs.jsonl` (task, lane, where, skill, tools used from the hook log, denials, result, seconds) | instant |
| **harvest** | the turn's own `<out>/lesson.json` (both prompts ask for it before the last line: a skill, a tool verdict, a note) -> `skills.write` + `tools/meta.json` | the same minute; the owner's reply ends "Learned: skill 'X' saved" |
| **score** | per tool: runs / ok / failed / last error; a tool in a blocked run is `needs_fix` and the next prompt says "FAILED last time, fix it first" | instant |
| **repeats** | the same kind of computer job done by hand >= 2 times (keyword clusters) -> ONE `agent_run` "build a tool for this" ticket, never twice per cluster | next tick |
| **repair** | a `needs_fix` tool -> ONE "fix this tool" ticket per failure | next tick |
| **digest** | evenings, one line in the owner's chat when something was learned | daily |

**Self-paced.** `pressure()` counts the opportunities (unharvested lessons x2, tools needing a repair x2,
repeated jobs without a tool x2, failures and heavy denials in 48 h, run volume) and `pace()` picks the
level: **hot** (score >= 6: tick every 10 min, 4 jobs a day), **busy** (>= 3: 30 min, 3), **steady**
(>= 1: hourly, 2), **idle** (0: every 6 h, no jobs). The spine fires `learn-tick` every 10 min; the
engine itself decides whether a tick is due. An own build or repair job that came back BLOCKED backs
the pace off for a day (the opportunity is there, the hands cannot use it yet). Rails: skills go
through `skills.write` (secrets and permissions refused), the engine never runs code, never answers a
card, never sends; it queues the same gated `agent_run` tickets an owner's `do` would, and each one
reports in the owner's chat.

```bash
python assistant/doer/learn.py --status
```

```bash
python assistant/doer/learn.py --pace
```

```bash
python assistant/tests/test_learn.py
```

## The code checks for a connector; the doer never refuses on that ground (2026-10-20)

The CRM connector and Google are reached through connectors the code already has (the CRM connector's exec module for its message, participant, stage, note and project-update kinds; `agent/google_api.py` for calendar events and guests), and Sage routes those kinds itself before a job is queued for the doer. From 2026-10-18 to 10-20 `prompt.md` told the doer to stop with `DOER_BLOCKED: this is a connector job` when sent such a task; nothing re-routed those tickets (doer.py only stages them), so Wren-on-a-project was refused three times in a row and a team member-as-Vision-Meeting-host once, for settings no connector covers. The rule is gone: the prompt now says the code already checked, and `doer.py` re-queues ONE such refusal as `new` (note `wrong refusal`) so the next turn does the job. A control that times out
twice is forced once through `browser_evaluate` (a DOM click on the snapshot ref; allowed in `doer.py` since
2026-10-19 for this only, `browser_run_code_unsafe` stays denied) and then given up with the page state, never
looped on. Code holds that line: `click_guard.py` (a Pre/PostToolUse hook `run_turn` passes with
`--settings`) counts the stuck clicks per control and, on the third, allows only a snapshot or screenshot, so
the job ends with the page state and the controls it could reach instead of at the 80-step limit. A job that is not done after about 20 browser actions stops with a plain report of the page. A run
that ends on its step limit or a crash reaches the owner as one plain sentence (`doer.plain_error`), never
the raw JSON.

A Google Sheet tab is never clicked (the price sheet's season_multipliers tab timed out on 2026-10-18 and
10-19): the doer reads every tab's name and gid from the loaded sheet with one `browser_evaluate`, navigates to
`.../edit#gid=<gid>` (the Name box as fallback) and lists the gids in its report for the sheet's skill.

## Milestone notes, and carrying on from them (2026-10-28)

the owner, after the Lensrentals cart died at step 80 with "nothing was changed" (2026-10-27, 9:30 PM): "example sends
multiple replies as she thinks things through. Sage tries to one shot it and runs out of steps." The property OS's
desk writes a one-line note at each milestone, and a resumed turn reads the notes back; that is ported here, org-free
(`notes.py`), for the browser lane:

- **The note.** The turn runs `python assistant/doer/report.py note --ticket <id> [--kind progress|blocked|done] "<line>"`
  at each milestone (signed in, dates set, each item as it lands in the cart, the form at its review page), when
  blocked, and before a checkpoint. `prompt.md` section "Milestone notes" says what a milestone is (three to six a
  run, not one per click) and what never goes in one (a password, a code, an id, a URL). Code appends it to
  `out/doer/<ticket>/notes.jsonl`, rewrites the `Now:` line of the live status (what Sage answers "where is it?"
  with), and records it on the job's transcript held off Chat (`receipts.note` with cap 0: Mission Control shows it,
  Chat never does, so no step-by-step lines and no prices in the team space staff read; Chat hears the job once, when
  it finishes or stops at its checkpoint; 2026-10-28, `doer.notes_per_job_max` is no longer read). The
  allow list carries `Bash(python assistant/doer/report.py note:*)` (gate baseline re-frozen).
- **Carrying on.** `doer.carry_on`: a run that ends on its step limit or its time limit (`cut_off`) is not a
  failure yet. Code posts one "Still on it, picking up from where it stopped: <last note>" line, then runs the job
  again with a `CARRY ON` block (why it stopped, try n of N, the notes oldest first, "carry on from the LAST note,
  never redo a noted step"): in the SAME session when the CLI returned one (the page and the cart are as it left
  them), else fresh with the block as the resume note. Up to `doer.continues_max` (3) more tries; the browser stays
  open between them. Out of tries, the owner's line says "it ran out of steps 4 times over, even carrying on from
  its notes. It got as far as: <the turn's last note>", and the ticket note says the same. A run that stops on its
  own (DONE, BLOCKED, a checkpoint) is never continued; the prompt's 20-action rule now reads "20 actions on ONE
  step", so a job that is moving may run on to the limit and be carried on.
- **Sage's chat reply is still one message** (the owner 2026-10-07: one answer, no robotic reports). The notes, the
  code's "Still on it" line included, are the job's own record on Mission Control and the status line.
- **The computer lane has the same** (same day, the owner: "Build it, we will manage chat noise later"):
  `computer_prompt.md` carries the Milestone notes section, `computer_policy.classify` judges
  `python assistant/doer/report.py note ...` as tier A whatever its words say (a note reading "deleted the 412
  cache files" is a sentence, not a command; only a report.py under `assistant/doer/` counts), and
  `computer.run_ticket` runs `doer.carry_on` with its own turn (the `DOER_COMPUTER_TICKET` env the hook needs)
  and the place line "the files, folders and tools are as you left them". `tests/test_computer.py` 7b.

Tests: `tests/test_doer.py` section 11, `tests/test_receipts.py` section 6. Config: `config.json -> doer.notes_per_job_max`,
`doer.continues_max`.

## Not built yet

- A live first run. The nested CLI cannot authenticate from inside a Claude session, so the first
  real browser task happens the first time an owner asks Sage for one; the browser, the vault, the
  login filler, the cards and the drain path are tested end to end without the model.
- Per-site playbooks (known selectors, known flows) learned from runs. Today every site is worked
  from its accessibility snapshot.
- Native Chat buttons on checkpoint cards (cards are answered on Mission Control; the chat push
  there carries the answer buttons).

### browser.py is the doer's own and is not scanned (2026-10-30)

`computer_policy.classify` scans the script a command runs; `browser.py` speaks CDP over a plain socket to bring the
browser on screen (`--front`), and the scan read `import socket` and refused it ("never: socket"), so a hand-off window
stayed hidden off-screen while the owner asked three days running for a window he could see. `browser.py` now joins
`report.py` and `login_fill.py` as the doer's own scripts the scan skips (its arguments are still judged; a browser.py
from anywhere else is scanned as before). The builder had filed this as a protected-core question twice; the owner's
"Fix the safety hook" / "do it do not fail" was the answer. Tests: test_computer 7b. Checkpoint lines say "with a
screenshot" only when the card carries one, and point to this chat only (`doer._card_has_screenshot`).

### A block that is not the owner's step is built, not reported (2026-10-30)

The owner: "Sage should be able to do anything I ask it even if it needs to stop, immediately build something, and not
update me at all until it works. I hate seeing 'Still working', 'That didn't work'." A `DOER_BLOCKED` whose reason is
not an owner-only step (`doer._owner_only`: a password or saved login, a verification code, a tap, a payment, identity)
and a run that ends with no result now say nothing in chat: `doer._gap_for` opens the capability gap for the ticket's
own ask and files its build (capability.file_build, builder kicked at once); the record goes to Mission Control
(`_mc_note`); when the build lands the ask is replayed by the capability loop and the owner hears the result. An
owner-only block is said once as "One step needs you on "<job>": <why>" (the portal line), never "I could not finish".
Tests: test_doer (blocked, no result, `_owner_only`), test_voice (the `progress` class holds "Still on it",
"I couldn't finish", "That still didn't take" back from Chat).
