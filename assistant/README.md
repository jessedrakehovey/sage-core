# Assistant - wake-on-work operator

the owner's personal assistant. Wedding videography (the organization) is one lane of four:
`wedding`, `personal`, `home`, `money`. Built 2026-09-21.

> **Talking to it (2026-10-05):** the conversational agent the owner and owner chat with is **Sage**, in
> [agent/](agent/README.md): a read-only brain whose proposals (rules, memory, wiki notes, schedules, calendar
> events) are applied by code, with outward work handed to the operator below as a `task`.

## Design: no standing lanes

Traffic is low, so nothing loops. Three tiers:

1. **Sensors (zero tokens)** - plain Python. `mail.py sense` (IMAP, read-only),
   `sense_local.py` (secret-iCal calendar + wedding project dates),
   `mail_arm.py` (money + home lanes: a bill with a due date, a renewal notice or a shipment
   with an expected date in a mail ticket arms the matching follow-up; conservative, only
   when a future date parses),
   `sense_editor.py` (edit bay: each D:/WeddingEditor project's pipeline gate that waits on
   the owner, idle projects with runnable stages, and the editor drive's free space; each ticket
   carries the project's newest word from memory / the tracker / the owners' chat so the nudge names
   the step the owner is really on, and a newest word saying not to nudge him (within `hold_days`, 14)
   means no ticket and its open edit tickets dismissed, 2026-10-21),
   `sense_honeybook.py` (reads the HoneyBook snapshot `dashboard/data/honeybook.json`
   that `hb_snapshot.py` refreshes every 2 h, 07:00-22:00, through the READ-ONLY connector
   (spine job `hb-snapshot`; haiku as transport only, the script parses + validates the tool
   result and installs it atomically; until 2026-10-01 only the Ad Scout pass refreshed it,
   twice a week, with a stale task list; since 2026-10-07 a connector timeout only marks
   HoneyBook down after 3 misses in a row, and the connector gets 90 s to start; since 2026-10-11 it also follows
   claude's newer "Output has been saved to <file>" note for an oversized result, whose rewording read as
   "no snapshot" for 3 days and looked like a lost login; since 2026-10-25 it reads only the HoneyBook
   call's own result, never another tool's, and a miss says why - connector not connected, tool never
   called, or no snapshot in its result - instead of quoting the transcript's tail): new inquiries, quiet
   leads, shoots entering prep, one weekly overdue-tasks ticket, one weekly balance
   ticket; also seeds `project_state.json` with booked future projects; since 2026-10-08
   `lead_meetings.py` matches HoneyBook's upcoming meetings/calls to the quiz leads, writes
   `meeting` + `meeting_note` onto `data/leads/<token>.json` so drafts confirm the booked call
   instead of asking them to schedule one, and posts one "Add to calendar" card per new meeting;
   a lead with a booked meeting gets no proposal nudge, reply chase or quiet-lead ticket at all
   (`lead_meetings.for_unit()`, checked when the follow-up fires); if the meeting passes with no
   outcome, the first held nudge fires a day later and the rest are closed),
   `sense_typeform.py` (2026-10-01: new Package Finder quiz leads straight from the Typeform API
   every 10 min via the Day Spine; prices each with `pricing/engine.py`, mints an
   `hb_proposal_draft` exec ticket so a priced DRAFT of Videography Collection Details lands in
   the couple's HoneyBook project, adds an `inquiry_new` ticket with the brief for a team member, arms the
   2 h reply SLA and posts a card; token = User env `TYPEFORM_TOKEN`; `config.json -> leads`).
   **2026-10-06 standing rule:** weekend (Sat/Sun) wedding + date open in HoneyBook -> after the
   draft lands, `collection_autosend.py` (called from drain.py) re-checks the date and queues ONE
   source-bound `agent_run` for the doer to Publish & share it with the "Custom Package email (quiz
   version)" template, no card; held 21:00-07:00. Any mismatch on screen falls back to a checkpoint card.
   A draft whose HoneyBook write times out or errors is **read back** (`hb_exec.py readback`, read-only)
   instead of dead-ending: one unsent file made by that run with every quoted price = done, auto-send
   proceeds; anything else stays an exception for the owner (example, 2026-10-19). Each adds
   id-keyed tickets to one queue and exits.
2. **Operator (Sonnet, only when the queue is non-empty)** - `operator_cron.ps1`
   starts one headless `claude -p` run that claims tickets, dismisses noise, stages
   reply **drafts** into Gmail Drafts, and stages decisions for the owner. Empty queue =
   no run, 0 tokens. `-Morning` adds the daily brief.
3. **the owner** - reviews drafts in Gmail and the **mission-control page**
   (`assistant/web/index.html`, rebuilt by every tick; `python assistant/scripts/dashboard.py --open`).
   Every card and note is also pushed to the owners' Google Chat DMs with Sage, with answer buttons
   (`chat_push.py`, 2026-10-05; Telegram removed 2026-10-04). Mission Control keeps the same cards with edit. the owner sends.
   **Stacked cards (2026-10-17; off under the weekly digest since 2026-10-20, see "Weekly approvals digest"):** a new top-level card that finds other open cards in the space takes their
   messages down and posts ONE message with ONE drop-down (example's one-fold shape: the title and a what's-inside line
   per lane stay visible; Show more opens every decision under its own head line, with its buttons); every button
   still answers its own card and a tap re-renders the stack. Cards asked for in a thread stay in that thread and
   never stack (the operator passes `cards.py post --thread <payload.thread>`). Switch: `config.json -> push.fold_multi`.
   State: `data/agent/chat_stacks.json`. Tests: `tests/test_chat_stack.py`.
   **A stack holds only what still waits (2026-10-17 evening):** the card just tapped shows its outcome (Undo stays), every
   other decided card leaves the drop-down on the next re-render, one left = the plain card (the owner: "a dropdown with every
   decision I ever made ... I only want multiple approvals on one card if they stack on top of each other").
   **The button grammar (same evening, `cards.py post` guards):** every button is something the SYSTEM does on the tap or a
   Skip/Drop (`NEEDS_ACTION`; a lone "Got it" is `NOT_A_DECISION`); what only the owner can do, or a fact only he has, is a
   plain line in Sage's voice (`cards.py say --lane <lane> "..."`, also a Mission Control note), never a card; a reply card
   carries its draft (`TEXT_NEEDS_DRAFT`, `OPTIONS_ARE_DRAFTS`); the same decision open (`DUPLICATE_SUBJECT`, by header +
   `subject`/entities) or answered in the last 20 h (`ALREADY_ANSWERED`, quoting his answer) is refused. The same holds by PERSON
   whatever the header (`scripts/decision_keys.py`, 2026-10-19: an open card about that lead/couple, a Drop still standing until
   HoneyBook moves, a nudge offer within 3 days of an approved message, the same draft already open or already sent to that
   number/address); sense_honeybook leaves dropped leads out of its tickets. Tests: `tests/test_repeat_asks.py`. Dead connections are
   carded BY CODE (`sentinels._conn_card` -> `cards.connection_card`: Set it up now / Leave it off / Something else; the
   sweep's `_act_on_setup` carries the tap out: connect.py's chat steps or setup_actions' recipe, said in chat; a connection with a chat setup gets "Say 'set up plaid' in chat" (`setup.chat.say`) and a
   detail with no command line or item id, 2026-10-18). Chase nudges
   are ONE daily line (`tracking.json -> chase.nudge: line`), dream questions, the no-fix weekly check and money FYIs are
   `say` lines. Sage side: an owner's ask for a message (`claims.asked_for_message`) must come back with a card-making
   SUGGEST; the bridge retries once, then queues the task itself (`forced`), so a card follows every time. Chat layout:
   labelled sections "The decision" / "What you're approving", "Draft · to X · goes out exactly as written", "Details".

Every ticket carries a `lane`. Today one operator drains all lanes. If one lane's
queue wait ever hurts, run a second operator with `tickets.py claim --lane <x>` -
a config change, not a rebuild.

## Safety properties (protected core - do not weaken without the owner)

- `mail.py` speaks **IMAP only**: no SMTP import, no send path. Its single write is
  APPEND to the account's own Drafts, addressed only to the ticket's original
  sender (the operator supplies a ticket id + body, never a recipient). The append
  is verified by Message-ID before a ticket may be staged as "draft ready".
- The inbox is opened `readonly=True` - sensing never marks mail read.
- The operator runs with an explicit `--allowedTools` list (not
  `--dangerously-skip-permissions`) because it reads untrusted email: Read/Glob/Grep,
  Write only under `assistant/out/`, and Bash only for `tickets.py` and
  `mail.py fetch|draft`. No web, no MCP, no git, no Edit, no subagents.
- Email content is wrapped as UNTRUSTED DATA; the prompt forbids acting on
  instructions inside it and forbids pricing/date commitments.
- All queue writers hold `QueueLock`; tickets are addressed by id, never position.
- Secrets live in Windows Credential Manager (service `the owner-assistant`), set only
  by the owner via `setup_creds.py`. `assistant/data/` and `assistant/out/` are
  git-ignored (they hold personal mail metadata).

## Turn it on (the owner)

```
python assistant/scripts/setup_creds.py mail personal       # Gmail app password
python assistant/scripts/setup_creds.py calendar personal   # secret iCal URL
python assistant/scripts/setup_creds.py status
powershell -File assistant/scripts/operator_cron.ps1 -SenseOnly   # sensors only, no LLM
python assistant/scripts/tickets.py list
```

Then run `operator_cron.ps1` once by hand from a terminal and read
`~/.claude/assistant-operator.log`.

**Scheduling:** task XMLs are ready in `assistant/tasks/` (UTF-16, as `schtasks /xml`
requires) but **not registered**. Register them only after a clean supervised run:

```
schtasks /create /tn "the organization" /xml "{{ORG_ROOT}}\assistant\tasks\AssistantTick.xml"
schtasks /create /tn "the organization" /xml "{{ORG_ROOT}}\assistant\tasks\AssistantMorning.xml"
```

Tick = every 30 min, 7:45 AM-9:45 PM (an empty queue costs 0 tokens). Morning brief =
7:15 AM. Both use `IgnoreNew`, so a slow run never overlaps the next one.

Add a second inbox (e.g. a the organization address) by adding an entry to
`config.json -> mail.accounts` and storing its secret.

## Comms lane (2026-10-06) - never miss a message, reply within minutes

Ported in shape from the property OS's comms agent after the owner said he liked that it "never
misses incoming communication and is quick to reply to text messages". Three zero-LLM pieces
around the same operator:

- **`comms_probe.py`** - scheduled task "the organization", every minute 07:00-21:30
  (`tasks/CommsProbe.xml`). When a claimable ticket of a human kind (`imessage_in`, `email`,
  `inquiry_new`, `card_answer`, `the owner_request`) is younger than 30 min and not yet woken for, it
  starts `operator_cron.ps1 -Comms` at once. Guards: the `.operator_running` in-flight marker (one
  operator at a time, also written by the half-hour tick), `min_gap_min` 3, the window, the daily
  budget. Log: `~/.claude/comms-probe-wv.log` (quiet probes write nothing).
- **`comms_state.py`** - the substrate: one record per thread (phone threads + mail / text
  tickets + open cards + verified sends) with class (`data/comms_law.json`: lead 15 min, couple
  2 h, vendor 24 h, owner 4 h, personal none), state (needs_reply / awaiting_the owner /
  awaiting_party / closed) and SLA band. A thread past its SLA with no card open gets ONE
  `sla_breach` ticket + a Chat note (latched). Rebuilt by the probe every minute and by the spine
  every 10 (`comms-state`). `data/comms_state.json`.
  An owner telling Sage they replied themselves (phone, HoneyBook: a memory fact by the owner or owner,
  about that contact, after their last text) makes the thread `awaiting_party` with `owner_replied`: no
  reply card and no chase; their next text opens it again (example, 2026-10-25).
- **`text_gate.py`** - wedding-only texts on the owner's iPhone (2026-10-08): a text there becomes a
  ticket / card only if the number is a lead, a booked couple, a phone in `Wiki/Couples` or
  `Wiki/Vendors`, or a HoneyBook client (then laned `wedding`). Friends, family and group chats
  stay in the thread log only: no card, no SLA, no unconfirmed-send sentinel; Sage still drafts
  any thread on request. Family is never wedding work even if a source lists them (2026-10-10:
  the owners, a HoneyBook contact on an owner's email, phones in `Wiki/People/Family and Friends.md`).
  Their texts are never ticketed on any phone or lane (2026-10-11); Mission Control restarts itself
  when any script module it has loaded changes, so a gate fix is live within 10 minutes.
  Since 2026-10-11 this covers owner's iPhone and every lane too: only a wedding client, lead or
  vendor number ever makes a reply card; anyone else is logged and drafted only when asked in chat.
  That chat-asked draft is one card, once: if it expires unanswered its ticket closes instead of
  being re-posted (`tickets._spent_text_exception`, 2026-10-13).
  A number in a `Wiki/People/` note tagged `family` or `friends` counts as family even when it is
  a past client, and a newer text card to the same number on the same phone closes the older open
  one as superseded (`text_gate.supersede_older`, 2026-10-15). A text card's hint copied word for
  word from another person's card (AJ got example's "gift exchange" hint) is swapped for a plain
  "Edit & send changes the words first." (`text_gate.own_advice`, 2026-10-28).
  Our own team (an `org.json` person who is not an owner: Marlo, example, a team member) always comes through,
  laned `wedding` on either phone; a reply card to someone texted from a card in the last 6 h posts
  into that card's Chat thread (`chat_push.reply_thread`, Marlo 2026-10-24).
  `config.json -> phone.triage_all_texts: true` turns every text back on.
- **`whois.py`** - who is this number (2026-10-23). One answer with the evidence for the bridge, the
  operator and Sage: owners, aliases an owner taught in chat (`data/people_aliases.json`, Sage's `identify`
  deed), the iPhone books (`contacts.py`), `Wiki/People` (table rows and "## the owner's dad" headings),
  `Wiki/Couples`, `Wiki/Vendors`, Sage's memory facts, the quiz leads, the tracker, HoneyBook's contacts, and
  the texts themselves (the name the phone sent, "Hey example" in the owner's own texts, "example:" and
  "Hey, it's Dave" signatures, group texts stitched from tapbacks that quote another member, vocatives
  inside the group, short codes / codes / promos / campaign markers, a number one digit from a known
  person's). `resolve()` -> name, relation, kind, confidence, evidence, candidates, group, hints, label;
  `find("my dad")` ranks people from a description; `directory()` is Sage's PEOPLE WHO TEXT block;
  `harvest` (spine job `whois-harvest`, hourly, and every inbound) writes sure names into the thread log.
  `phone_bridge.inbound` titles the ticket with it and carries `payload.who`. CLI:
  `python assistant/scripts/whois.py who <number|"my dad"|"the mechanic"> | directory | harvest | learn | stats`.
  On 2026-10-23 the log had names for 9 of 59 threads; the index names 19 sure + 16 likely, 19 automated.
  Tests: `tests/test_whois.py`.
- **`text_confirm.py`** - a reply proves the text went out (2026-10-10): when a number texts in
  after an approved text to it was handed to the phone but never acked, that text is marked sent
  and any open "text unconfirmed" card for it closes as resolved. Runs on every inbound text and
  as the spine job `text-confirm`.
- **`comms_page.py`** - the board at Mission Control `/comms` (`/api/comms` = JSON).
- The operator prompt's "Comms first" section says how to work them. Tests: `tests/test_comms.py`.

## Weekly approvals digest + urgent cards (2026-10-20)

the owner: "New design for approval cards since I'm getting overwhelmed with them. Help reduce unnecessary
decisions for me. I only want a drop down long list of cards once a week and making a decision on those
should be the final close to each unless they are not confirmed done. I need to have an approval to text
new prospects ASAP when a new one comes in with phone number. Normal approvals will be singular cards from
now on besides one drop down card with them all together once a week."

- **Every card is held** (`config.json -> cards.delivery: "weekly"`): `cards.post` still runs every guard
  and writes the card to the store at once - it is open on Mission Control and in Sage's transcript the
  minute it is posted (answer it there any time) - but **nothing reaches Chat** until digest day. The post
  result says `delivery: weekly` and its advice says so; the operator / Sage must never tell the owner a held
  card is "on your phone".
- **The digest** (`scripts/digest.py`, spine job `cards-digest` every 10 min, slot `cards.digest_day` +
  `digest_hour`, Monday 08:00): ONE collapsible Chat message per owner space (example's one-fold shape, title
  "This week's approvals · N waiting", a visible lead line, then every held card under its own head with
  its question, the text that would go out, advice, details and **its own buttons**). More than
  `max_per_message` (10) = numbered parts posted together. Last week's digest message comes down first
  (everything still open is in the new one). A tap answers through the same guarded path as any card and
  re-renders the message without the decided card (the one just tapped keeps its Undo). The digest is
  computed from the slot, so a spine that was down at 08:00 posts at its next tick and never twice.
  `digest.py status | preview | post` (post = now, the owner's call). State `data/cards/digest_state.json`.
- **Urgent cards go to Chat at once, each on its own, never stacked** (`push.fold_multi` is ignored under
  weekly delivery): a poster's `"urgent": true`; a header starting with one of
  `cards.urgent_header_prefixes` ("New lead", "Text not confirmed"); a card asked for in a chat thread (the
  owner is waiting on it); a revision of a card that already reached Chat; a standing text's preview card
  (`hold_text`: time-bound, gone by itself before any digest).
- **The new-prospect text is the urgent card** (`texts.card_kinds: ["lead_intro"]`): the minute a lead with
  a phone lands (quiz or HoneyBook inquiry) `client_texts.lead_intro` posts "New lead: <couple>" - the
  intro text verbatim under `[TEXT PREVIEW]`, the lead brief (date, venue, budget, recommended collection,
  what happened in HoneyBook) in its details, **Send now / Skip**; his tap mints the sha-bound
  `imessage_send` (drain holds it 07:00 if it is night). One card per number, ever (the dedupe record is
  written before the card). The "Hot lead - what next?" card is only posted for a lead with no number.
  The other five standing texts still go by themselves.
- **A decision is final.** ALREADY_ANSWERED + decision_keys stop any re-ask; a tap on a decision card is
  carried out by the operator with no card back. A digest card nobody decided rides along
  `carry_over_weeks` (1) more digests, marked "2nd week, drops after this", then expires and is **dropped**:
  no `card_expired` ticket, no re-post, its staged ticket is closed. The sentinel never reports a held card
  as stale. A held card the digest never showed (spine down 3 weeks) expires at `DIGEST_HOLD_MAX_S`.
- **Week-old items ride the digest, never a mid-week line** (2026-10-20 14:46: "three things have sat for a
  week ... say 'nudge the cold leads'"). `sense_honeybook.weekly_card` posts ONE held "Cold leads" card and
  ONE "Outstanding balance" card a week by code (no more leads_cold / balance_outstanding tickets): Draft the
  nudges / Check the open invoices hands the work to Sage as one ticket, Leave keeps those leads off next
  week's card. The cold count leaves out leads dropped on a card, leads Sage's memory records as dropped,
  skipped or lost, and leads with a booked meeting. A ticket staged 5+ days is a held "Waiting Nd" card
  (`sentinels._staged_card`: Hand it to Sage / Close it), not the daily chase line. Before that card (2026-10-26)
  an ask that already landed closes quietly (a later finished ticket or a memory fact shows the same names and
  change done), so does an expired card about a number memory flags as a scam, and rewordings of one job
  ride ONE card ("asked N times"; Close it closes them all) (`sentinels._settled`, `_same_job_groups`). An ask an
  owner closed closes quietly too: a Skip / Drop tap on its card, or a later never-raise rule, 'stop following up' or
  'handling it himself' line naming the same person and matter (`_owner_closed`; a team member's camera 2, the insurance retry). Tests:
  `tests/test_weekly_nudge_cards.py`.
- **"Unless they are not confirmed done":** when drain hits a PERMANENT failure on a card-approved ticket
  that provably did not go (never an UNVERIFIED result - the message may have landed, the operator checks
  first), `cards.reopen` brings back the SAME card, open, its question headed "Not confirmed done last
  time (...). Tap again to retry.", its old answer voided like an undo. Urgent -> straight back to Chat;
  weekly -> next digest. At most twice; the failure line lands under the old card's thread.
  `cards.py reopen <qid> --why "..."` by hand.
- Switch back: `cards.delivery: "now"` (every card to Chat at once, the mid-week stack returns).
  Tests: `tests/test_cards_digest.py` (+ `test_chat_stack.py` pins the old mode, `test_client_texts.py` §2).

## Scheduled sends and scheduled jobs (2026-10-24)

the owner: "I need to be able to schedule text sends and everything else I've asked it to do today."
Same chain, one new field. An exec ticket may carry **`not_before`** (`YYYY-MM-DD HH:MM`, local):
`drain` leaves it queued until that minute (`exec_store.due`; the pass reports it as `waiting`), undo
cancels it until it runs, the sentinels do not count it as stuck, and the identity / resend / blank
guards apply at mint time as always.

- **A scheduled message.** Sage's `task.draft` takes `send_at` ("text her tomorrow at 9" -> the card is
  posted NOW with "Sends at Sat Oct 25, 9:00 AM" in words and in the Chat footer; the tap mints the
  ticket with `not_before`). `intents._check_draft` refuses a time in the past, more than 90 days out, or
  inside quiet hours (21:00-07:00). Any card may carry `exec.send_at`; `cards._sanitize_exec` normalizes it
  and insists the time is on the card in words (EXEC_VERBATIM), and `_answer_locked` passes it to
  `exec_store.enqueue(not_before=...)`. A tap after the time has passed mints nothing (the answer records
  `mint_error`): a new card, not a late send.
- **A conditional send (2026-10-28).** "Text example at 4 if he and example haven't replied": the draft (or the
  `approve` deed) carries `hold_if_reply` with its time; `actions._remember_hold` stores the condition by card in
  `data/send_conditions.json` (who, and HoneyBook's questionnaire / meeting state at that moment). The tap is still
  needed. `scripts/send_hold.py` (spine job `send-hold`, long group, right before `drain`) checks every such ticket
  due within the next pass for anything from them since the card was made: a text (phone threads / `imessage_in`),
  a mail from any address the project ledger has for the couple or a HoneyBook notification naming them, an
  `hb_message_in`, the questionnaire now completed, a new meeting on their project. Anything -> the ticket is
  cancelled and one "Not done: I held this back because ..." line goes in the card's thread; nothing -> the drain
  sends it as approved. The CRM checks are the plugin's (`crm_honeybook.reply_watch` / `reply_baseline` /
  `reply_evidence`), so the engine file stays org-free. Tests: `test_send_hold.py`, `test_send_hold_crm.py`.
- **A scheduled local job.** `assistant/jobs.json` is the allow-list of fixed commands with named values
  (today: `handoff_build`, `handoff_return` - the editor-handoff skill). Sage's `job` tool
  (`{job, args, at?}`, JOBS YOU CAN RUN in its context from `jobs.render`) posts ONE card
  (`actions.job_card`: exec kind `local`, payload `{op, args}`, `send_at`); the tap mints the ticket;
  when due, `drain._exec_local` calls `jobs.launch`, which starts the command DETACHED through
  `job_runner.py` (a build renders for hours) with its output in `data/jobs/<key>_<stamp>.log`; the runner
  says the outcome in chat and on Mission Control (`cards.say`, owner `jobs`) and ledgers it. No shell, no
  free-form command: an op not in the registry is only recorded, as before. Add a job to `jobs.json`,
  never to code. By hand: `python assistant/scripts/jobs.py run handoff_build couple="example"`.
- Tests: `test_cards_exec.py` (scheduled card, waiting ticket, undo, past time, job card) and
  `test_intents.py` (send_at in quiet hours, job tool, the cards both post).

## Approval chain (Phase 2, 2026-09-30) - cards, tickets, drain

Ported from the property OS's question_bridge + executor, trimmed to this OS. Zero LLM
anywhere in the approve-and-execute path.

1. **Card** - the operator posts a decision card (`cards.py post`), after `rulings.py find`
   (the owner's standing rulings, `data/rulings.json`). Guards: NO_RECOMMENDATION, freshness
   (already replied / 3rd card to one recipient in a day), exec payload verbatim in the
   question, exec keys valid, identity allow-list. Store: `data/cards/` (live / superseded /
   archive, `answers.jsonl`, `notes.jsonl`). The operator can post, note and list; it is
   denied `answer`, `undo`, `harvest`, `drain.py`, `email_send.py`, `exec_store.py`.
2. **Answer** - the owner taps: `python assistant/scripts/cards.py answer <qid> --option "<label>"
   [--text "..."] [--revised-file f]`. Undo (`cards.py undo <qid>`) works until the answer takes effect (see "The undo is real"). On an exec card, the
   approve label mints a **sha-bound executor ticket** (`exec_store.enqueue`, approved_by=the owner,
   approval_ref=card:<qid>`). Kinds: `email_send`, `calendar_hold`, `local`. Duplicate guards:
   same recipient/thread within 90 min, identical payload within 30 d.
3. **Drain** - every tick `cards.py sweep` (expire, harvest, free-text answers -> tickets) then
   `drain.py` re-verifies each ticket's sha and hands the exact payload to a registered
   script. `email_send.py` is the ONE send path (Gmail SMTP with the account's app password;
   from must be owner4@example.invalid or owner3@example.invalid, never example).
   A reply carries the parent's References + Message-ID and "Re: <original subject>"; the sent
   copy's Gmail thread is read back and a reply that landed in a new thread is said so in the result.
   Outbound held 21:00-07:00. Failures: PERMANENT -> exception (+ an `exec_exception`
   ticket), transient -> retry x2.
4. **Ledger** - `data/audit_ledger.jsonl` is hash-chained: `exec_store.py verify-ledger`.
5. **HoneyBook writes** (2026-09-30) - kinds `hb_note` (internal note on a workspace) and
   `hb_project_update` (date / location / details / guest count) run through `hb_exec.py`: the
   TypeScript is built from the approved payload by a fixed template, and one headless
   `claude -p` allowed ONLY `mcp__honeybook__take_action` carries it to the connector. No
   template deletes. A failed or unverified write goes straight to exception (never blind-retried).
   **`hb_message` (2026-10-15)**: a reply to a couple inside HoneyBook (`CommunicationsAdapter.createCommunication`,
   email channel, threaded on their last message by `in_reply_to_id`), sent by HoneyBook from the owner's
   account the moment he taps Approve. OUTBOUND like an email (quiet hours, resend guard); the card
   must carry `[HONEYBOOK MESSAGE PREVIEW]` + the body verbatim. Built after the owner asked for a
   HoneyBook reply to example and the operator could only offer "paste it in yourself".
   When the connector refuses it, the words go through HoneyBook's web composer (`scripts/honeybook/hb_send_message.py`);
   once the thread shows them, any other still-queued `hb_message` with the same words to that couple is marked done
   with that evidence and its open card closed, so the drain never sends it twice (example & example, 2026-10-29).
   Moving a pipeline stage stays manual.
   **Evidence after a send (2026-10-15)**: when drain finishes any send (`email_send` Message-ID,
   `hb_message` HoneyBook message id + time, `imessage_send` hand-off, other `hb_*` record id),
   `drain._report_result` posts one line in the tapped card's own Chat thread (`chat_push.push_result`)
   and a card note, so "sent" is always backed by the record (the owner 2026-10-14: confirm sends with
   evidence). The tapped card's mark follows it (2026-10-24, example): ⏳ while the send waits,
   ✅ only when that evidence line comes in, ❌ when it was refused or failed ("Not done ..." or
   a mint error), ⚠️ when the read-back disagrees (`chat_push.answered_icon`).
   **Not yet proven live**: the first real run happens on the owner's first approved HoneyBook card
   (the nested CLI cannot authenticate from inside a session; dry-run + templates are tested).

Exactly-once rules (2026-10-01 hardening; each only tightens the path):

- **One card, one question, one answer.** `post` refuses more than one question (the page
  only ever showed the first). `answer` refuses a card that is not `open`: a double tap or a
  second approver cannot mint a second ticket; a change of mind is `undo` or a new card.
- **Everything approved is visible.** from / to / cc / subject and the WHOLE body must be in
  the question, and the page shows the whole question.
- **The undo is real.** `drain` holds any ticket approved on a card for 35 s, whoever calls
  it; standing-approval tickets (the internal lead alert, the quiz draft) are not delayed. A
  running ticket can no longer be cancelled.
- **Undo works until the thing has happened (2026-10-20).** It used to die after 30 s, while the
  button stayed on the card, so the owner's 08:35 Undo on a 09:00 intro text was refused. Now
  `cards.undo`: an approved send can be undone while its ticket is `queued`; a text already in the
  phone outbox but not yet fetched by the phone is recalled (`phone_bridge.recall` +
  `exec_store.mark_recalled`); one that ran says plainly that it went out and can't be pulled back. A
  decision (no send) can be undone up to `UNDO_DECISION_MAX_S` (24 h), even after the sweep consumed
  it: the card comes back open in the same Chat message, an operator ticket nobody started is closed,
  one already started (or a Hold / setup / build / pending step code already ran) gets a `card_undo`
  ticket telling the operator to reverse it. The next answer's ticket id carries the card's `round`,
  so it is not deduped away. Tests: `tests/test_undo.py`.
- **Uncertain means stop.** `email_send.py` prints `NOT_SENT` (before hand-over: retried),
  `SENT=1`, or `DELIVERY_UNVERIFIED`. Drain retries only the first; an unknown failure, a
  timeout, or a ticket orphaned by a killed drain becomes an exception ("check Sent mail" /
  "check the HoneyBook project"), never a second send. One drain at a time (`.drain.lock`).
- **A tap is an instruction.** A tap on a decision card (2+ options, no exec) becomes a
  `card_answer` ticket the operator carries out; an expired card nothing tracks becomes a
  `card_expired` ticket.
- Still open, the owner's call: the word "resend" in a card disables the duplicate guards
  (`Memory/skill-suggestions.md` ## Pending).
- A further message an owner asks for in chat to someone who already got one today (or has
  one queued) says RESEND on its card by itself (`actions._resend_mark`), so the first tap
  sends; the identical wording and cards the system raised still hit the duplicate guards.

Tests: `python assistant/tests/test_cards_exec.py` and `test_email_send.py` (scratch dirs via
`ASSISTANT_DATA` / `ASSISTANT_CARDS`). Phone-answerable cards arrive with Mission Control
(roadmap Phase 4); until then the morning brief and `assistant/web/index.html` list open
cards with the answer command.

## The outcome ledger: "done" means read back (WP1, 2026-10-24)

the owner: "take action with verifiable results and never let a failure go unnoticed without a follow-up".
Until this, "done" meant a ticket was consumed, a card was tapped, or a model said so: a sheet write whose
read-back said `VERIFIED=no` was done, a text handed to a phone that never acks got a green check, 27 tickets
staged "asked in chat" never closed. `assistant/scripts/outcomes.py` owns ONE row per claim and moves it to a
state a read of the record justifies: `claimed -> verifying -> verified | unverified | failed | abandoned`
(`assistant/data/outcomes.jsonl`, append-only, folded; every change is a link in the audit ledger).

- **Executors prove, by kind** (`outcomes.verdict_from_exec`): `email_send` needs `SENT=1 <Message-ID>`; `hb_*`
  the record id and its read-back token (`MEMBER=yes`, `VERIFIED=yes`, `ARCHIVED=yes`); `sheet_set`
  `VERIFIED=yes`. A hedged read-back (`MEMBER=no`, `VERIFIED=no`) is a **failure**: `drain.py` marks the ticket
  `exception` (`PERMANENT: READBACK: ...`), the card comes back for a retry, nothing is marked done. A
  text (`imessage_send`) is only `claimed` at hand-off; its verifier reads the phone outbox: a reply or an ack
  verifies it, `expired`/`recalled` fails it, and past the phone's no-ack window (`HANDED_STALE_NOACK_MIN`, 4 h
  on a phone that has never acked) it is `unverified`.
- **An unverified row asks the owner ONCE** (`outcomes.ask_owner`): an urgent card "Did this text go?" with It went
  (Recommended) / Send it again / Drop it. The tap is the evidence (`cards.sweep -> _act_on_outcome ->
  outcomes.answer`): It went marks the outbox item sent by the owner; Send it again fails the row and brings the
  ORIGINAL card back worded RESEND (`cards.reopen(resend=True)`, so the same text can be minted again); Drop
  abandons it. Nothing answered after 72 h is abandoned with one line. A card the guards refuse rides one `say`.
- **Marks and closes read from the ledger.** `chat_push.answered_icon`: ⏳ claimed/verifying, ✅ verified, ⚠️
  unverified, ❌ failed. `cards.close_answered` no longer closes a ticket on the tap when the exec's row is still
  in flight; a `failed` row whose card came back keeps the ticket staged on it; "asked in chat" tickets close
  after `CHAT_ASK_H` (48 h). The doer's DONE writes a row too (`doer._outcome_doer`): checked evidence =
  verified, no evidence file = unverified (the browser lane now writes `evidence.json` like the computer lane;
  `doer/prompt.md`), a mismatch = failed. The agent's hand-offs mirror in (`commitments._outcome_open/_close`),
  and a completed-action sentence in Sage's reply with no deed behind it is a `said` row that fails at once
  (`bridge._ledger_words`, the property OS's exec_verify rule): the scorecard counts them.
- **Spine job `outcomes-settle`** (every 10 min) runs the due verifiers, asks, abandons, and speaks in the card's
  own thread (`chat_push.push_result`) or the hand-off's receipt. `python assistant/scripts/outcomes.py open|failed`
  are the zero-LLM views (`/open`, `/failed` in chat land with WP2).

Tests: `assistant/tests/test_outcomes.py`. Proposal: `assistant/BUILD_PROPOSAL_SAGE_STANDALONE_2026-10-24.md`.

## The follow-up engine: nothing fails unnoticed, once (WP2, 2026-10-24)

The property OS's lanes never let a failure sit: a channel that fails twice is paused, a route that runs past its
clock is changed, and a dead cron is noticed from outside the machine. Ported, tools only:

- **A circuit breaker per send channel** (`drain._breaker_note`, `assistant/data/breaker.json`): two TRANSIENT
  failures in a row on one lane (email, phone, honeybook, sheets, ads, doer) trip it; the rest of that lane is held
  with attempts untouched, ONE "<channel> paused" card is posted by code (`cards.breaker_card`, Try again now /
  Leave it until it recovers), one probe ticket runs per pass, and the first success closes it and clears the card
  (`clears_when: {"breaker": lane}`). PERMANENT failures are the payload's fault and never count.
  `python assistant/scripts/drain.py --reset-breaker <lane>` by hand.
- **Route clocks** (`tracking.json -> routes`, minutes per route class: connector 15, phone 240, browser 90,
  operator 150, build 1560, person 2880): an outcome row that has waited past its route's clock becomes
  `unverified` and the owner is asked once (`outcomes.settle`), instead of waiting again. A failed standing send
  (a quiz draft, a standing text, a triage note: no card to come back) gets ONE "Did not go" card by code with
  Retry it / Drop it; Retry mints a fresh ticket with the card's provenance.
- **Sentinel findings are cards by code**, not `stuck_work` tickets (`sentinels._plan`, `cards.sentinel_card`): a
  failing spine job (Run it now), a stalled spine or a failed scheduled task (Start it now), a quiet sensor (Run the
  sensor now), an approved item still queued (Run the drain now / Cancel it), dreaming or a self-test (Run it now /
  Hand it to Sage), each urgent, each clearing itself when the check passes (`clears_when: {"sentinel": key}`,
  DUPLICATE_ALARM keeps it to one). The chase findings (an open card, a staged ticket, an unconfirmed text) are ONE
  plain line a day in Sage's voice ("Still waiting on you: ..."). No LLM run stands between a failure and the owner.
- **The spine cannot starve itself** (`spine.py`): a pass has a budget (`PASS_BUDGET_S`, 17 min inside the 20-min
  kill); a job that would not fit is skipped this pass and runs first next time. `doer-tick` and `drain` are
  `"group": "long"` and run from their own task, **the organization** (`spine.py --group
  long`, every 10 min, 25-min limit, `assistant/tasks/SpineLong.xml`, `spine_long.ps1`). A job that declares
  `"expect": "<prefix>"` (the sweep, the drain, the sentinels, the settle) must end with that line; exit 0 without
  it is `silent` and counts toward `fails`.
- **The deadman** (`assistant/scripts/deadman.py`, stdlib only, imports nothing from `assistant/`; task **example
  Deadman (assistant)**, every 30 min, `assistant/tasks/Deadman.xml`): the Day Spine, the chat bridge, the comms
  probe and disk space, each with its window; a stall is said through `cards.py say`, then the owner's own email
  (`config.json -> deadman.email_fallback`, default on), then a dashboard card + `assistant/data/deadman_alert.json`;
  latched 6 h, the recovery said once. `deadman.py --status`.
- **The owner's own line is on a clock** (`bridge.stalled_turns`): an owner message still in flight after 45 min gets
  one plain line where it was asked and a failed `turn` row in the ledger. `/open` and `/failed` in chat list what is
  not confirmed and what did not go (zero-LLM); the morning brief carries an UNCONFIRMED sentence (`connect.py brief`).

Tests: `assistant/tests/test_escalation.py`.

## Steps read the record before they fire (WP3, 2026-10-24)

The property OS's follow-ups fire on time or on an email, and its world model only ranks; ours did the same, so a
questionnaire reminder went to example for a questionnaire he never had and a chain gate counted "the reminder fired"
as "the step happened". Now:

- **`action.satisfied_when`** on a SOP step (`data/sop_map.json`) is read by `assistant/scripts/preconditions.py`
  in `followups._fire` before anything else: `satisfied` = the item is done `by_record` (no ticket, no text);
  `moot` (the ledger says cancelled / lost) = dismissed; `open` / `unknown` = it fires with what was read as its
  evidence (`record checked: ...` / `record unreadable: ...`), so the operator's ticket starts from the record.
  Checks: `questionnaire_answered` (a completed questionnaire file on their HoneyBook project), `timeline_on_file`
  (the ledger, the wedding page, or a completed questionnaire), `outbound_since` (`days` or `since: anchor`; an
  email / HoneyBook message / text to them, read from the exec ledger and not failed in the outcome ledger),
  `touches_sent`. Shipped on questionnaire-reminder (both), timeline-request, thank-you, week-of-confirm,
  prep-checkin, finishing-touches. `python assistant/scripts/preconditions.py <check> <unit>` by hand.
- **Every fired step is an outcome row** (`followups._outcome_followup`, verifier `followup`): by record =
  verified, moot = abandoned, fired = claimed until its ticket is completed (verified) or dismissed (abandoned).
- **Chains stop trusting a fired reminder** (`tracking/chains.py`): a follow-up `done` counts as evidence only when it
  was done by record or its ticket was completed (`any_done: true` on a rule keeps the old reading).
- **Two more inbound facts** (`sop_map.inputs`): `file_received` (a sent timeline / list closes the timeline
  request, a HoneyBook note, the couple page) and `yes_to_proposal` (a note; the quote follow-ups close; the
  proposal / contract step is the operator's reply). `chain_book` tickets have their own section in
  `prompts/operator.md` (before this nothing told the operator what one was).
- **An unconfirmed text is not a missing reply** (`comms_state.build`): a text handed to the phone and not yet
  confirmed makes the thread `awaiting_party` with `unconfirmed_out`, never a false `needs_reply` SLA and never a
  false "verified"; the outcome ledger's own card settles it.

Tests: `assistant/tests/test_followups.py` (8c), `test_triage_next.py`.

## Learning that measures itself (WP6, 2026-10-24)

- **The scorecard** (`assistant/scripts/scorecard.py`, zero-LLM): per kind of action, how many claims reached a
  verdict, the share confirmed landed, the median time to confirm, retries; the failure classes (credential,
  timeout, connector, browser, phone, refused, model-claimed); cards posted / answered / edited / skipped / expired;
  the owners' repeat-or-correction lines; sentences Sage said with nothing behind them. ONE plain paragraph under
  the Monday digest (`digest.post`) and a row in `data/scorecard.jsonl` + the eval ledger (pipeline `scorecard`),
  so the trend is visible. The nightly review gets the same lines (`dream.prepare`, channel `scorecard`) with the
  instruction to propose at most one rule, skill or build request against the worst of them.
- **Conversion watch** (the property OS's loop_health): every card source (the operator, the sentinels, the chase,
  the chains, the ledger, the standing texts) is scored generated / acted / discarded; a source whose cards are
  under 10% acted on is IGNORED, one that produced nothing for 7 days after producing before is DEAD; both are
  named in the scorecard and the review may propose switching them off.
- **Lessons get an eval**: each `assistant/learnings/<lane>.md` line names the card it came from; the scorecard
  counts edits of that card kind in the 30 days before and after; a lesson that did not reduce edits is flagged
  ("no effect") for the review to reword or retire.
- **Graduation, offered never assumed** (`assistant/scripts/graduation.py`, `tracking.json -> graduation`; the
  property's followups/graduation.py scaled down): an allowlisted internal exec kind (`hb_add_participant`,
  `hb_set_stage`, `sheet_set`, `hb_note`, `hb_project_update`) with `min_samples` (10) resolved taps, 90%+ approved,
  zero edits, zero undone and zero failed outcomes in 90 days is offered ONCE a month as a card ("Make it
  automatic?", Make it automatic (Recommended) / Keep asking). The tap grants it (`cards._act_on_graduation`), and
  `triage_next` then queues that kind under the standing ref `standing:graduation <kind> (owner tap <ts>)`; the
  outcome ledger verifies it like anything else and the first failed outcome demotes it (spine job `graduation`,
  daily). Sends, money, the doer's steps, prices and dates are `FOREVER_GATED` whatever the file says.
- **Regression packets** (`scripts/evals/packets/*.json`, `assistant/scripts/regression_packets.py`): five owner
  lines that once went wrong, each with the deeds a good turn makes and the phrases a good reply never contains.
  `--check` is zero-LLM; `--live` runs each through `chat.py` and records PASS/FAIL per packet in the eval ledger,
  so a prompt or rule change that gets worse shows as a regression. Not yet on a cron (needs the User-env token).

Tests: `assistant/tests/test_scorecard.py`.

## Chat-contained: the channel seam and the slash surface (WP4, 2026-10-24)

- **`assistant/channels/`**: the chat channel behind one interface (`base.Channel`: post / post_body / patch /
  patch_body / delete / get / find_dm / thread_of / to_markup / configured). `channels.gchat.GChat` wraps the live
  `agent/gchat.py` one to one (same module object the bridge holds, so auth and message names are unchanged);
  `channels.slack.Slack` is the stub that proves the seam (every method, `render_card` turns a Chat card body into
  Block Kit with a button per option carrying the same `{qid, option}`); `NullChannel` posts nowhere and remembers
  everything (tests, a new organization with no channel yet). `channels.get()` picks by `agent/profile.json ->
  channel` (default gchat; `ASSISTANT_CHANNEL=null` in tests) and is what `chat_push._g()` returns, so every card,
  result line, digest and say already goes through it. The bridge's own inbound path (Pub/Sub pull, taps) still
  imports `gchat` directly: that is the remaining WP4 work for a second channel.
- **Slash surface** (`bridge.command`, zero-LLM): `/open`, `/failed` (WP2), `/evidence <card>` (the tap, the
  ledger state, the Message-ID / HoneyBook id / outbox id), `/who <number or name>` (whois.explain), `/switch <job>
  on|off|run` (switches.py, owners only), `/digest now` (post the weekly drop-down early), `/quiet until <time>` /
  `/quiet off` (no chases, outcome asks or stall lines until then; cards still post and urgent ones still reach Chat;
  honoured by `outcomes._quiet` and `commitments._window_ok`), `/signin <site>` (opens the agent's browser on the PC
  screen at that site through setup_actions and hands over the Setup page). `/help` lists them all.
- **Not built:** a true sign-in from the phone (a screenshot cannot take input; it stays a build request), and
  Mission Control's edit pencil / note box as chat equivalents.

Tests: `assistant/tests/test_channels.py`.

## The org pack: one file makes it someone else's (WP5, 2026-10-24)

- **`assistant/org.json` + `assistant/orgcfg.py`**: the values the CODE needs before anyone has taught the agent
  anything, in one file: the send identities (`exec_store.ALLOWED_FROM`), the phones it may text from and the
  default (`phone_bridge.ACCOUNTS`), the Mission Control URL (`chat_push`, `comms_page`, the doer), the scheduled
  tasks the sentinels watch, the internal addresses and alert defaults (`sense_typeform`), the phone Shortcut's
  name. Each module reads `orgcfg` with its old literal as the fallback, so this organization behaves exactly as
  before. `org.json` is protected core (`builder.PROTECTED_PATHS`): the identities are what the send gate guards.
  What the agent LEARNS about the business stays in `agent/state/org.json` (org.py).
- **`new_org.py` stands up a tree that boots.** It now copies the data a new organization cannot run without
  (`sop_map.json`, `comms_law.json`, `client_texts.json` as templates, an empty `rulings.json`, `spine_jobs.json`),
  writes the new `org.json` from the owners, rewrites the PowerShell wrappers and task XMLs to the new root and the
  new task names, leaves this organization's skills, learnings, tests and planning documents behind, and scrubs
  this organization's names, addresses, host and folder out of every data, prompt and doc file (code comments keep
  their history). `assistant/tests/test_new_org_boot.py` proves it: Acme Bakery scaffolds in one command, its
  spine lists its jobs, its ledger, cards, follow-ups, deadman and sentinels run, `exec_store` resolves Acme's
  identities, and nothing of the organization is left in its data.
- **Still WP5 work (not built):** the service adapters (`services/crm.py` over `hb_exec` + the snapshot, forms,
  phone, calendar, mail) and the host adapter (schtasks / PowerShell / Credential Manager): today a second
  organization without HoneyBook edits `sop_map.inputs`, `tracking.json` and the HoneyBook-specific sensors
  (`sense_honeybook.py`, `client_texts.py`, `hb_exec.py`) rather than swapping one adapter. The generic SOP
  template is the wedding one with the names scrubbed; a bakery edits its steps.

## Lifecycle autopilot (Phase 3, 2026-09-30) - follow-ups + sentinels

`scripts/followups.py` + `data/sop_map.json` (ported from the property OS's Follow-Through
engine). After step A of an SOP happens, a follow-up is ARMED (date trigger with `T-14d`
style offsets against an anchor, a mail trigger, or manual); when it fires it becomes an
assistant TICKET (kind `followup`) whose `payload.instruction` tells the operator exactly
which step to do and which skill to use. Recurring steps re-arm (`recur.every_days`, capped)
until cancelled (`followups.py mark <id> --status done`). Dedupe is on sop+step+unit across
every status except expired, so sensors can re-arm on every pass.

SOPs shipped: `wedding-lifecycle` (timeline request T-30, prep + check-in T-14, week-of
confirm T-3, ingest gate T+1 daily, thank-you T+2, edit-start T+7, delivery check weekly
from the promised date or wedding + `projects.default_turnaround_weeks`, review request
T+14 after delivery), `inquiry` (2 h reply SLA, proposal nudges at 48 h and 7 d), `money`
(bill due, quarterly estimate, renewal), `home` (delivery arriving, recurring maintenance),
`personal` (reminder, awaiting-reply mail trigger).

Producers: `sense_local.py` arms the wedding chain for every tracked, undelivered project;
`sense_honeybook.py` arms the inquiry SLA for new inquiries and the proposal nudges for
leads at "Proposal Sent". `followups.py tick` and `sentinels.py` run every operator tick
after the sensors. `sentinels.py` is the stuck-work watchdog: staged tickets > 5 d, cards
open > 36 h, approved exec tickets queued > 6 h, exec exceptions, quiet sensors -> ONE
daily digest ticket, latched for 2 days per item. An item an earlier digest still covers
(its ticket not yet carded, or its card still open, in any lane) is skipped, never re-carded.
A staged item live status shows is already fixed is dropped before it reaches a card (the HoneyBook
connector is refreshing, or memory records calendar sync on), and no finding line carries terminal or
/mcp steps: it points at Setup on Mission Control instead (2026-10-16).

Hardening (2026-10-01): `followups.StateLock` serialises arm / tick / mark; a bare-date anchor
means 09:00 local that day; a moved anchor re-times a duty that has not fired; a busy queue
defers a fire instead of losing it; `followups.cancel_unit` stops a cancelled or delivered
wedding's chain (called by `sense_local.arm_lifecycle`). `cards.sweep` closes the ticket that
was staged on a card once the card is answered, hands a ticket back to the operator once when
its card expired unanswered or it was staged with neither a card nor a draft, and spills typed
answers it cannot queue. The sentinel digest id is day + problem set, so a new problem the same
day still pings.

Tests: `python assistant/tests/test_followups.py`.

### Standing client texts (2026-10-17) - the end of the pipeline, texted

the owner: "It is my goal and vision for the entire process to be completely automated with texts."
`scripts/client_texts.py` + the words in `data/client_texts.json` (his voice, his sign-off; edit
the words there, never in code). Zero-LLM: each kind mints a sha-bound `imessage_send` from
`the owner-iphone` under the standing ref `standing:client-texts`, deduped per kind + number in
`data/client_texts_state.json`; drain / the phone outbox / quiet hours work as for any text, and
every send is a Mission Control note. Switches: `config.json -> texts` (all, per kind).
**Heads-up before every text (2026-10-18):** Sage says in the shared chat (`cards.say`, not a card)
the full wording, who it goes to and exactly when. No standard text goes 21:00-09:00: one minted
then waits in the state file's `scheduled` (no ticket), the heads-up names 9:00 AM, and the
`client-texts` spine job (every 10 min, before `drain`) mints it at 09:00. `status` lists the waiting ones.
**Lead time + "Hold this one" (2026-10-18 evening, plan section 4):** the kinds known days ahead
(`config.json -> texts.preview_lead_kinds`, default questionnaire_reminder / checkin / meeting_reminder /
touches_followup) go no sooner than `texts.preview_lead_min` minutes (default 60) after their preview, inside
the same 09:00-21:00 window (`client_texts.planned_send`). Every text that waits (lead time or overnight) is
previewed as ONE card (`_preview_card`: header "<kind>: <couple>", `[TEXT PREVIEW]` with the full wording and
the number, exactly when it goes, buttons **Hold this one** / **Let it go as planned**, `hold_text` = the state
key). It is not an approval: the text goes by itself at its time, and the card closes itself as "went out"
(`cards.close_card`). **Hold** (the sweep's `_act_on_hold`, or `release_due` reading the card at send time, so a
tap minutes before still counts) takes the text off the standing path and posts it as a normal `imessage_send`
exec card (Send / Skip, Edit & send); the dedupe record stays so nothing re-mints it. `client_texts.py hold --key
"<kind>|<who>"` is the same from a terminal. The meeting reminder's "tomorrow"/"today" is read at the send time.
The immediate kinds (lead_intro, collection_sent) keep the plain heads-up line and go at once.

| kind | when | who fires it |
|------|------|--------------|
| `lead_intro` | the minute a quiz lead with a phone lands ("I just saw your wedding form come through..."); also a HoneyBook contact-form inquiry whose contact arrived with the project (`texts.intro_on_honeybook_inquiry`) | `sense_typeform.handle` / `sense_honeybook.arm_inquiry_followups` (`_form_contact`). The hot-lead card then says it went and no longer offers to draft one; the 2 h reply SLA is not armed. |
| `collection_sent` | right after the Collection Details went out: the doer's standing send (`doer._after_standing` on DOER_DONE) or HoneyBook showing a lead moved to Proposal Sent within 2 days (`texts.collection_on_hb_stage`) | doer / sense_honeybook |
| `questionnaire_reminder` | wedding-lifecycle `questionnaire-reminder` (T-30) and `-2` (T-21): the operator checks the Wedding Questionnaire in HoneyBook, then runs `client_texts.py send questionnaire_reminder --unit <key>` (refuses a second send inside 7 days, and refuses unless the HoneyBook snapshot's `questionnaires` list shows a sent questionnaire file in their project; then the operator posts one card offering to send the questionnaire template with the scheduler link it finds itself) | followups -> operator -> CLI |
| `checkin` | wedding-lifecycle `checkin-text` (T-14), `action.auto: client_texts:checkin`: `followups._fire` sends it by itself and marks the duty done; only when it cannot (no number) does the operator get the ticket. Since 2026-10-24 it is signed "the owner" (no rose) and carries the Touch Point Call booking link instead of asking what time works (`texts.checkin_body` + `texts.checkin_scheduler_link`, an https honeybook.com / hbportal.co link); with no real link it is held and the operator posts ONE card | followups auto hook |
| `meeting_reminder` | the day before (16-30 h ahead) every HoneyBook meeting / call with a client (the meeting's own client phone) or a hold on our calendar whose title is a call / check-in / shoot with a known couple | spine job `client-texts` (every 10 min, 07-21) |
| `touches_followup` | after HoneyBook confirms the Finishing Touches file went out | `drain` (hb_touches_send done) |

**The Finishing Touches file (T-60, `finishing-touches`):** two months out (one month is too soon), the
touches a couple's collection does NOT include (full toasts & speeches film, aerial cinematography,
second cinematographer, iPhone content creator, feature film, raw footage, vows reaction angles,
couple's session) go out as a HoneyBook smart file built like the Videography Collection Details:
the **Finishing Touches** template (`58a564a164ef00679d925dc7`, designed 2026-10-18 through the
connector: every page rewritten for touches, 7 priced touches, no discount or payment plan, scheduler
optional; page deletes / tab names are editor-only, see `client_texts.json -> touches.template_copy._doc`),
choose-any selection, **prices hidden until checkout**,
picks flow to the invoice, due on the wedding date. Nothing client-facing says "add-ons" (the owner: it has
to sound upscale). The operator confirms the booked collection (HoneyBook read-only / the couple note)
and runs `client_texts.py touches --unit <key> --package <id> [--has ...] [--skip ...]`, which mints ONE
standing `hb_touches_send` exec ticket (OUTBOUND: quiet hours + resend guards) -> `drain` -> `hb_exec`
-> `pricing/hb_touches.py`: find the contact + project (never creates them), skip if a Finishing
Touches file was already published or sent, create from the template, set items + selection + schedule,
send with the owner's email (`createCommunication` + `smart_file_id` publishes the draft as part of the
send). Evidence `HB_DONE=<file> SENT=<message>`; then the follow-up text. Prices: `client_texts.json ->
touches.catalog.<id>.price` overrides the sheet; an item with no price anywhere (the toasts film until
the owner sets one) is left off and named in the ticket's `unpriced`.

Phone resolution (`resolve_phone`): the ledger entry's `phone`, then the quiz lead file (email, or
date + first name), then the HoneyBook contact (email, `workspace_ids` link, or the one contact
whose name matches). `hb_snapshot.py` now records `created_at` + `workspace_ids` per contact.
Owners' numbers are never texted; an inquiry is never texted a per-wedding kind.
Tests: `python assistant/tests/test_client_texts.py`.

## Tracking and reminding - example's chase, Sage's way (2026-10-15)

the owner: "look at how good example is at tracking and reminding and copy some things nearly 1 to 1 ... adapt it
to our uses". Decided in an 18-question grill ([TRACKING_DESIGN.md](TRACKING_DESIGN.md) holds every ruling);
everything tunable is in [tracking.json](tracking.json) so another organization gets it by editing a file
(`new_org.py` scaffolds it with the example-specific detectors off). Code: `scripts/tracking/`, zero-LLM:

- **`chase.py`** - one nudge card per lane per day about what waits on the owner (an open card past a day,
  a staged ticket past 3 days, a follow-up that fired and was never consumed), re-asked after a day of
  silence, folded after 3 asks. Default `chase.nudge: line`: ONE plain chat line a day per owner. With
  `nudge: card` the card leads with something the system does (button grammar): "Bring it back up" re-posts
  an open card, "Hand it to Sage" queues an operator ticket for a ticket/follow-up; then Handled / Remind me
  tomorrow / Drop it. A card the guards refuse rides the daily line instead (2026-10-19). Posted in the owner's
  measured active hours (`rhythm.py`, from his card answers and chat lines), never in quiet hours. Personal
  texts and the sentinels' own digests are never chased (ruling 13; `chase.exclude_tickets`).
- **`drift.py`** - the record says one thing, the evidence says it happened: the delivery ledger vs the
  editor, HoneyBook's stage after a wedding that passed, a bill reminder still armed after "payment
  received", a timed calendar event with nothing logged since. Cached every 3 h for the weekly card.
- **`weekly.py`** - **Friday** readback (what moved, what stalled, done-but-not-closed) to the shared
  space, then ONE "Weekly record check" card; its approve mints a **`tracking_fix`** executor ticket
  (`exec_store` kind, local lane, never outbound) and `drain` runs `drift.py apply`: a ledger stage or a
  closed reminder, nothing that reaches a person. HoneyBook stage moves stay the owner's hand.
- **`advice.py`** - the 💡 line now rendered on every card (`chat_push.card_body`, `card_line`).

Spine jobs `tracking-drift` / `tracking-chase` / `tracking-weekly`. State: `data/tracking/`. Tests:
`python assistant/tests/test_tracking.py`.

**The lifecycle layer (slice 2, same day; rulings 4, 6, 10, 11):**

- **`chains.py` + [chains.json](chains.json)** - declared next-step chains, example's `next_step` shape: a
  chain is an ordered list of gates, each with a due offset (from the wedding date, the day a lead
  reached its stage, or a tax date) and the evidence that satisfies it (ledger, HoneyBook, the editor
  pipeline, calendar, follow-ups, tickets, sends, the owners' own chat lines, the registers, the owner's
  tap). Four ship: **shoot** (timeline, second shooter, rentals, travel, cards; asked T-14 / T-7 / T-3),
  **edit** (ingested -> music -> first cut wk 5 -> review -> revisions -> delivery wk 8), **lead**
  (reply -> call -> proposal -> contract -> retainer, each with a window), **money** (quarterlies and
  tax season). The first gate not done whose predecessor is done or ending, with no evidence by its
  ask time, gets ONE card: Booked / Book it for me / Not yet / Drop (max 2 per lane per day). "Book it
  for me" mints a `chain_book` ticket for the operator / doer, never a send. Drop on a **lead** gate
  latches the lead: no later gate is carded and its proposal nudges (`inquiry` quote follow-ups) are
  dismissed, until HoneyBook shows a new stage or activity after the tap. A **cold lead** (its step due
  more than `stale_days` (60) ago, or no HoneyBook activity in that time) gets no booking card: ONE
  held "Archive this lead?" card for the Monday digest instead (Archive latches it; Keep asks as usual),
  and it drops out of the world model's open loops (2026-10-29). `chains.py gaps` prints
  every entity's chain; `--dry-run` the cards it would post.
- **`entities.py`** - the world model: one row per lead, couple, edit, money obligation, person, trip
  and appointment with stage, next required action, deadline, days overdue -> `data/tracking/entities.json`
  (git-ignored). Money and people start from follow-ups, `Wiki/People`, `Wiki/Vendors` and the
  **registers** `data/registers/{money,people,trips}.json` the owner fills from chat (`entities.py init`
  writes them with their shape documented inside).
- **`priority.py`** - the priority brain: importance = lane weight x (1 + log10($ at stake) + who waits
  [client > vendor > admin] + a hard date), urgency from the deadline (1.0 at it, 3.0 deeply overdue),
  an Eisenhower quadrant per row. `advice.score` hands over to it for any item that carries a deadline.
  **Q2** (important, not urgent) rows get one nudge line each in the Friday readback, never a card.
  Tunables in `tracking.json -> priority`.
- **`dayplan.py`** - tomorrow, lined up at `tracking.json -> dayplan.plan_at` (19:00), once a day, only
  when it has 3+ timed items (events, calls, deadlines, reminders, chases due back): ONE plain message
  to the shared space in Morning / Afternoon / Evening blocks, owner's line only on days she has
  items. Otherwise silence. `--dry-run` prints it.

Spine jobs `tracking-chains` / `tracking-entities` / `tracking-dayplan`. Tests:
`python assistant/tests/test_lifecycle.py` (56). `new_org.py` ships `chains.json` with the template.
The rest of the interview (rulings 3, 9, 12, 13) is the section right below; the SimpleFIN bank feed +
money lane are "Money lane".

### Mining, the personal-text exception, scheduling, probation (rulings 3, 13, 12, 9; 2026-10-15)

All zero-LLM, all in `scripts/tracking/`, every word list in JSON so another organization edits a file:

- **`mining.py`** (ruling 3; the shape of the property OS's `vendor_threads.py`) - the first pass over
  everything new since the last run: every inbound text (`data/phone_threads.json`, personal ones
  included, plus the `imessage_in` tickets the 60-per-contact thread log has rotated out), mail and
  HoneyBook inquiry tickets, and HoneyBook client messages when the snapshot carries them. The owners'
  own chat with Sage is not mined (2026-10-17, `sources.chat` off: Sage already hears those lines). A date beside a commitment word from a couple / planner / vendor /
  family / friend / owner becomes ONE `calendar_hold` card ("Date from Mom: ... Put a hold on the
  calendar for 10/18 at 5 PM?"). Holds are only for the owners' own plans (2026-10-28): a prayer request
  or a surgery / hospital line is someone else's day and gets none, and a line with no date borrows the
  previous line's day only when it names a time itself; a **money sender's** due / renewal / action-required / trial-ends /
  payment-failed date becomes ONE "Money dates" `tracking_fix` card whose rows are `followup_arm`
  fixes (`drift.py apply` arms `money/bill-due`, `money/renewal` or `personal/reminder` on the tap).
  `mail_arm.py` keeps arming plain bills / renewals / deliveries by itself (now through its shared
  `find_dates`); mining skips what it already classified and adds only `mining.json -> money.extra`.
  Never silent, never sends. Past dates, questions, quoted reply headers, codes and marketing are
  skipped; a date already on the calendar / in `holds.json` / on an open card is not carded again,
  and nothing is carded for a sender who already has an open reply card (the reply covers the ask).
  A lead's wedding date is never a hold (an inquiry is not a booking; a call they name still is),
  new-inquiry tickets and quiz-alert lines (`Flags:`, `Recommended:` ...) are not mined, and a message
  with nobody to name on the card makes none (2026-10-20, the ": Flags: ..." card). A number not in
  Contacts is titled by what Sage's memory or a wiki note says it is about ("General Liability insurance:
  Coverage will end on 10/26/2026 ... (unverified)"), never the raw number; `(unverified)` when a mention
  calls it unchecked or a possible scam (2026-10-19). Titles are cut at a whole word.
  Vocabulary + switches: [mining.json](mining.json). State: `data/tracking/mining.json`.
  `python assistant/scripts/tracking/mining.py scan --days 7` lists every finding in the window.
- **`text_gate.py`** (ruling 13) - personal texts stay logged-only EXCEPT a **time-related** one (a
  date or clock time AND a plan word) or a **money-tied** one (an amount, pay / owe / venmo), which
  may become a ticket and so a draft card, marked `payload.exception = "time" | "money"`. The owners'
  own numbers never come through; family and friends do (`include_family`). Switch:
  `config.json -> phone.personal_exceptions {enabled, time, money, include_family}`; words in
  `mining.json -> personal_text_exception`. The operator prompt says what to do with such a ticket.
- **`scheduling.py`** (ruling 12) - "set up a call with X" / a lead asking for a call: reads both
  owners' Google calendars (the agent's token) plus `data/calendar.json`, the HoneyBook meetings in
  the desk snapshot, `data/holds.json` and the times already offered to someone else; picks 2-3 open
  slots on different days inside `tracking.json -> scheduling.windows`; posts ONE card with the
  drafted message carrying them, exec `imessage_send` / `email_send` / `hb_message` as the thread
  dictates (templates per channel and audience in `scheduling.templates`, lint-clean, first names,
  no price / date / turnaround). The tick (`tracking-scheduling`, every 15 min) reads their reply:
  a picked time ("Tuesday works", "the 2 pm one", "option 2") -> the confirmation message card AND a
  `calendar_hold` card; "none of those" -> logged, left to the operator; no answer in
  `proposal_ttl_days` -> expired, slots freed. One open offer per person. A lead who already booked
  through the HoneyBook scheduler (`lead_meetings`) is never offered times. Triggers: the operator
  (`scheduling.py propose --ticket <id>` / `--with "<name>" --channel ... --to ...`, see
  prompts/operator.md "Scheduling a call") and Sage's SUGGEST `call` field (`agent/actions.py`;
  `claims.py` holds a reply that promises times to that field).
- **`probation.py`** (ruling 9) - the 20-right-in-a-row tally per class from the card answers joined
  to the cards and the executor ledger. Classes in `tracking.json -> probation.classes`:
  `record_write` (tracking_fix, calendar_hold, hb_note, hb_project_update, local) and
  `routine_client_send` (email / text / HoneyBook sends whose follow-up step is the thank-you, the
  ingest gate or the 48 h quote follow-up, or whose header says so; a reply to a lead never counts).
  AS-IS = the exec's own approve label, no edit, no typed note, no Skip, the ticket not undone;
  anything else resets the class to zero. At the bar ONE "Make it automatic?" card
  (Keep asking me (Recommended) / Yes, make it automatic), once per run; the answer is recorded in
  `data/tracking/probation.json` and nothing else happens. `python assistant/scripts/tracking/probation.py`
  prints the tally per class.

**Probation: how a flip is made by hand (the code never does it).** There is no switch to turn. A
"yes" on the card is a ruling, not a change: `exec_store.enqueue` still refuses anything whose
provenance is not the owner's tap on a card (`approved_by="the owner"` + `approval_ref=card:<qid>`), and
`check_gates.py` freezes that. To make a class automatic, a session, with the owner watching the diff:
(1) quote the ruling card's qid in the commit; (2) for `record_write`, let the posters that mint
that kind (`weekly.py`, `mining.py`, `lead_meetings.py`) call `exec_store.enqueue` with a new
provenance `approved_by="ruling:<qid>"` that `enqueue` accepts for the kinds listed in a new
`tracking.json -> probation.auto` list and for nothing else (`OUTBOUND_KINDS` stay tap-only unless
the ruling names `routine_client_send`); (3) for `routine_client_send`, additionally keep
`draft_lint`, the quiet-hours hold and the same-target resend guard on the path, and post a note
(`cards.py note`) for every auto send so the Friday readback shows it; (4) add the test that proves
every other kind is still refused, re-run `python scripts/guardrails/check_gates.py --freeze` only if
a gate file changed, commit scoped. Until that commit lands, every card keeps coming.

Spine jobs `tracking-mining` (30 min) / `tracking-scheduling` (15 min) / `tracking-probation` (twice a
day). Tests: `python assistant/tests/test_mining.py` (59), `test_scheduling.py` (43),
`test_probation.py` (26); `test_phone_bridge.py` covers the ruling-13 gate.

## Money lane - a read-only bank feed (2026-10-15)

Rulings 14-15 of `TRACKING_DESIGN.md`. `scripts/money/` is zero-LLM, **read-only on every bank** (never
pays, transfers or enters a credential), writes only under `data/money/` (git-ignored), and tunes
entirely from `money.json` (feed cadence, account-name -> kind patterns, Relay's split percentages,
card days, forecast tolerances, due dates, **tax tables to re-check every January**) and
`money_rules.json` (merchant regex -> bucket; `learned` rows come from the owner's typed answers).
Wiki note: `Wiki/Integrations/Bank Feed (SimpleFIN).md`.

- **Feed** `simplefin.py`: SimpleFIN Bridge ($15/yr; banks are linked on their site). The protocol has
  one single-use POST (setup token -> access URL, done by `setup_creds.py money simplefin`, stored in
  Credential Manager as `simplefin:access_url`) and `GET /accounts`; the script asserts method + path
  on every request. Incremental by transaction id into `data/money/ledger.json`, ~4 pulls a day of
  SimpleFIN's 24. Connection row `bank-simplefin` (status file) shows on `/setup` with Open page /
  Enter setup token / Check. **Relay is not on SimpleFIN** (MX; Relay supports Plaid only): that is the
  second adapter below.
  Its "down" card points to the Bank feed button on `/setup` (no command line), and once the Plaid feed is
  set up (`bank-plaid` not `not_configured`) a dead SimpleFIN feed gets no card at all (2026-10-15).
- **Feed (Relay)** `plaid.py` (2026-10-15, the owner's yes): Plaid, read-only by construction. `Client.post` allows
  only `/transactions/sync` (cursor in `data/money/plaid_state.json`) and `/accounts/balance/get`, plus
  `/link/token/create` + `/item/public_token/exchange` inside the one-time Link flow (`plaid.py setup`: a
  one-page site on 127.0.0.1 hosts Plaid Link, the browser opens, the owner signs in to Relay in Plaid's own
  window; no bank credential touches this code). Keys `plaid:client_id` / `plaid:secret` via
  `setup_creds.py money plaid` (which then offers the Link flow); access tokens `plaid:access_token:<item>`
  written by the flow, never printed. Same `merge()` contract and ledger rows as SimpleFIN (accounts carry
  `feed: plaid`); Plaid's sign (positive = out) is flipped, a pending row that posts hands its bucket to the
  posted row, Relay's buckets map by name through `money.json -> accounts.kinds`. `ITEM_LOGIN_REQUIRED` ->
  status error naming `plaid.py setup --update <item>`; `plaid.py unlink <item>` forgets the token locally
  (never `/item/remove`). Connection row `bank-plaid`; `money.json -> feeds` is now a list (one block per
  adapter; `cfg()["feed"]` still returns the SimpleFIN block).
- **Categorize** `categorize.py`: first matching rule wins (learned before shipped), own-account
  transfers paired, Relay buckets by account name. ONE weekly card (Friday after 15:00) numbers the
  unplaced rows; "1 software, 2 personal, 3 skip" typed in its thread becomes learned rules (keyed on
  the merchant handle) + a line in `learnings/money.md`, and the rows are placed.
- **Forecast** `forecast.py`: recurring charges -> upcoming bills **armed as `money / bill-due`
  follow-ups** (T-3d, no new reminder path); latched cards for a low balance (7-day bills > the paying
  account) and a price jump.
- **Quarterly** `quarterly.py`: YTD Schedule C profit -> annualized federal (SE, standard deduction,
  QBI, brackets) + CA; the next due date armed as `money / quarterly-estimate` (T-10d) with the amount
  in `data/money/quarterly.json`; a set-aside card after each HoneyBook payment lands, netting out
  Relay's split once `relay.splits_pct` is filled in.
- **Monthly** `monthly.py`: the 1st-of-month readback to the shared space via `agent/bridge.post`.
- **Relay from the phone, in chat** (2026-10-16, `scripts/money/plaid_wizard.py`): say "set up Plaid" or
  "connect Relay" and Sage answers (code, no model turn: `connect.py` + the `bank-plaid` row's `setup.chat`)
  with the steps and ONE link, Mission Control `/setup/plaid`. That page is a three-step wizard: Plaid
  sign-up (the owner's own), the two keys pasted straight into the PC's Credential Manager and proved
  with a Link token, then Plaid Link opening on the phone where the owner signs in to Relay inside
  Plaid's window. The page exchanges the public token, runs the first pull and posts "Relay is
  connected: ..." into the shared space. "Connect another bank" opens Link again for each further bank
  (2026-10-18); once keys are stored the page hides the sign-up step and the chat steps skip sign-up +
  paste and name the banks already linked (`setup.chat.state` + `{"if": ...}` steps in connect.py).
  Phone only, end to end (2026-10-20): the page links straight to Plaid's Keys page, has a Paste button
  on each key box, gives a Copy address + Open API page fix when Plaid wants the redirect URI, and turns
  a "sign in again" error into the page's Re-sign-in button; the chat steps never mention the PC.
  Plaid Link's mobile web flow (2026-10-21): the Link token is minted with the page's own https address
  as `redirect_uri` (`plaid_wizard.mint`); when Plaid has not been told that address yet it is minted
  without one so Relay and every other non-OAuth bank still link, `plaid_status.json -> oauth_ready`
  goes false and the page shows the Copy address + Open API page fix. The page keeps the token in
  localStorage and re-opens Link with the SAME token + `receivedRedirectUri` when an OAuth bank sends
  the phone back (`?oauth_state_id=`); the page fits 375px with no sideways scroll. The "paste what it
  gives you here" line after chat steps now belongs only to a connection with a chat `paste` block
  (`connect.paste_hint`), so the Plaid steps no longer ask for anything in chat.
  Long-lived keys never pass through chat; the SimpleFIN
  one-time token still may. Tests: `tests/test_plaid_wizard.py`.

Spine jobs `money-pull`, `money-pull-plaid`, `money-categorize`, `money-forecast`, `money-quarterly`, `money-monthly`.
Tests: `python assistant/tests/test_money.py` and `python assistant/tests/test_money_plaid.py` (offline; fake feeds, scratch stores).

## Why a text waited for the "Send now" button (2026-10-24)

the owner: "sometimes texts don't send until I push send immediately". The PC side was never the delay:
every tap minted its ticket the same second and drain handed the text to the phone outbox ~36 s later
(the undo window). The phone fetches within seconds **only when the example-SEND wake-up SMS reaches
it** (`phone_nudge.py`). The 12:19 text that waited 51 minutes is the one whose nudge logged "no smtp
secret for example": the card was answered under a Python without the `keyring` module, so
`alib.secret()` returned None and no SMS went. Two fixes, both zero-LLM:
- `alib.secret()` falls back to the wedding-web venv in a subprocess when `keyring` is not importable,
  so a card answered from ANY interpreter (the operator's `python`, the CLI, a test) still nudges.
- `phone_nudge.renudge_if_waiting()`, called by the comms probe every minute: a text still `ready`
  (never fetched) 2 minutes after the last nudge gets another wake-up, up to 5 per waiting spell,
  never in quiet hours; the counter resets when the outbox empties. The deferred re-nudge timer inside
  the answering process dies with that process (a CLI answer exits at once), so the probe is the path
  that survives. Tests: `tests/test_phone_bridge.py` (re-nudge block).
Still phone-side: the "the organization" shortcut has never POSTed `/api/phone/ack`
(`phone_bridge.acks_ever`), so the PC cannot tell WHEN the phone sent; step 5 of
`the owner_IPHONE_SETUP.md` (Get Contents of URL -> /api/phone/ack) is missing or failing on the phone.

## Mission Control (Phase 4, 2026-09-30) - answer cards from the phone

`scripts/mc_server.py` serves the mission-control page on `http://127.0.0.1:9019` and, through
Tailscale Serve, at **https://<this-pc>.<tailnet>.ts.net:9443** (tailnet only: the owner's own
devices; nobody else can reach it). Zero LLM at runtime. It only does what `cards.py` already
lets the owner do from the CLI: answer / undo a card (`POST /api/cards/answer`, `/api/cards/undo`)
serve the Desk at `/desk` (HoneyBook strip, leads, edit bay, nagging; links both ways),
and queue a request for the operator (`POST /api/ask` -> ticket kind `the owner_request`, which the
operator answers with a card on its next tick). Every card on the page has its option buttons,
a note box, and a 30 s undo after a tap. If the full page cannot be built (a malformed
state file), the server serves a safe-mode page with just the open cards and their buttons.
Card answers and asks must be same-origin JSON; the phone token never reaches the log. Runs from the logon task **"example Mission Control
(assistant)"** (`assistant/tasks/MissionControl.xml`, launcher `mc_server.ps1`, single
instance). Disable the tailnet exposure with `tailscale serve --https=9443 off`.

## owner's lane - iMessage via her iPhone (2026-10-01)

Fifth lane, `owner`: takes the stress of replies and upcoming dates off the owner's wife. This
machine is Windows-only and iMessage has no API, so her iPhone does the messaging through
Apple Shortcuts talking to Mission Control over Tailscale (`scripts/phone_bridge.py`; phone
setup: [owner_IPHONE_SETUP.md](owner_IPHONE_SETUP.md)).

- **In:** a Message automation POSTs `/api/phone/inbound` -> thread log
  (`data/phone_threads.json`) + ticket kind `imessage_in`. The operator reads the thread
  (`phone_bridge.py thread <handle>`, read-only), posts an `imessage_send` EXEC card in her
  voice (`[TEXT PREVIEW]` + recipient + exact text verbatim), and arms `owner/heads-up`
  follow-ups for dates (gift 14 d, RSVP 3 d, appointment 1 d before).
- **Out:** Approve (the owner or owner; `by` records the Tailscale login) mints an
  `imessage_send` ticket (lane `phone`, account allow-list `owner-iphone`, duplicate windows
  2 min same contact / 1 d identical text). `drain.py` puts it in `data/phone_outbox.json`;
  her "the organization" shortcut GETs `/api/phone/outbox` (which first drains phone tickets
  older than the 30 s undo window), sends with Send Message, POSTs `/api/phone/ack`.
- **At most once:** an item is `handed` on fetch and never re-offered. Handed-but-unacked
  after 30 min becomes a sentinel finding (check her Messages before re-sending).
- **Door:** every phone call needs the token in `data/phone_token.txt` (`phone_bridge.py token
  [--rotate]`), on top of Tailscale. Outbound held 21:00-07:00 like email.
- **Email:** her Gmail `p49c139@example.invalid` is mail account `owner` (`"lane":
  "owner"` routes all of it to her lane) and on `exec_store.ALLOWED_FROM`. Idle
  (NOT_CONFIGURED) until her Gmail app password is stored: `setup_creds.py mail owner`.

Tests: `python assistant/tests/test_phone_bridge.py` (30 checks, scratch dirs, includes the HTTP token door).

## HoneyBook -> Wiki/Couples sync (2026-09-30)

`scripts/honeybook/hb_couples_sync.py` runs nightly from `dream_cron.ps1` (and on demand):
every booked, not-completed HoneyBook project within the last 60 days or in the future gets a
`Wiki/Couples/YYYY-MM-DD <Lastname>.md` note. Existing notes are matched by `honeybook_id`
(or adopted when a hand-written note on that date has no id yet); only the
`<!-- hb:begin -->...<!-- hb:end -->` block and `updated` change, so the owner's and dreaming's
text is never touched. New notes are `status: draft`. Lints, then commits its own notes.

## Day Spine (Phase 5, 2026-09-30) - one zero-LLM housekeeping task

`assistant/scripts/spine.py` + `assistant/spine_jobs.json`, fired every 10 min 06:00-23:30 by
the task **"the organization"** (ported from the vault's night_ops/runner.py). Each
job has its own cadence, window and timeout, and a state row in `data/spine_state.json`
(`spine.py --list`). The task only fires 06:00-23:30, so a job `window` must sit inside those
hours (rescue-kit sat at 02:00-06:00 and never ran until 2026-10-01). `sentinels.py` reads the
state file: a job that fails 3 runs in a row, a spine that stops firing, or a scheduled task
whose last run exited non-zero becomes a stuck-work finding. Jobs: hb-snapshot, cards-sweep + drain (10 min: approved sends leave within minutes,
not at the next LLM tick), mc-keepalive, followups-tick, sentinels, heartbeat, gate-health,
graphify, log-rotate, dashboard-rebuild. It replaced four separate Windows tasks (Graphify,
Heartbeat, Gate Health, Mission Control keepalive); their XMLs stay on disk. Log:
`~/.claude/spine-wv.log`. The operator tick (LLM) and dreaming remain their own tasks.

## Connections from chat (2026-10-16)

the owner: "make it stupid simple, preferably from inside Sage chat with links and steps." `scripts/connect.py`,
zero-LLM, driven by a `setup.chat` block on a `connections.json` row (`ask_words`, `steps`, `paste`): an owner
who says "set up simplefin" (or `/connect simplefin`) gets the numbered steps with links back at once, and the
one-time setup token they paste is recognised by code (`detect`: a base64 claim URL), claimed and stored through
`setup_creds.py money simplefin --stdin` in the keyring venv, the first pull runs, and the token is scrubbed from
the transcript and inbox files (`scrub`). The bridge does all of this before any model turn, so the token never
reaches the model. A one-time token is the ONE kind of secret allowed in chat; passwords, app passwords and codes
stay on the Setup page. The CONNECTIONS block tells Sage which rows are chat-settable. A general bank ask
("can I set up a bank account?") that never says SimpleFIN gets the Relay/Plaid steps instead while either bank
feed is down or never set up, ending with one line on how to ask for SimpleFIN (`defer_to` / `own_words`,
2026-10-17). Tests:
`python assistant/tests/test_connect.py`.

## Connection health (2026-10-02)

`assistant/connections.json` is the registry of every external connection (MCP, credential,
login, CLI): where its credential lives by name only, how it is checked, how it is renewed and
by whom. `scripts/conn_check.py` (zero-LLM, read-only, spine job `conn-check`, hourly) proves
each one and writes `data/connections_status.json`: `live`, `dead`, `not_configured` or
`manual`. Mission Control shows them in the **Connections** panel; `sentinels.py` turns any
`dead` connection into a finding with the fix. A never-configured connection is not a
finding (it is backlog: [CONNECTIONS.md](../CONNECTIONS.md)). Details carry error types only,
never a secret. The old HoneyBook browser-login probe (`dashboard.py --hb-check`) is retired:
HoneyBook signs in through Google, so the connector is the path and its snapshot age is the
health signal. Add a row to the registry in the same change that adds a connection.
**The brief's CONNECTIONS line (2026-10-18 evening, plan section 2):** `python assistant/scripts/connect.py brief`
prints ONE sentence naming what is not connected (or stopped working) and the fix the owner taps or says for each:
the row's `setup.chat.say` phrase when it has one ("say \"set up plaid\" here"), else the Setup-page button
(`setup_actions.recipe` label). Which rows: `config.json -> brief.connections` (ids, in order; today personal
Gmail, personal calendar, owner's Google, Plaid, NEXT Insurance); a row's `brief_name` is the plain name used
there. `prompts/brief.md` copies the sentence verbatim (omitted when all is well), so these stop surfacing as
mid-task failures. New check type `cmd_match` (output regexes `live` / `missing`) backs the `next-insurance` row
(`vault.py has login.nextinsurance.com`; Setup button "Sign in" opens the doer browser on NEXT's sign-in page).

Tests: `python assistant/tests/test_conn_check.py` (scratch dir) and `test_connect.py` (the brief line).

**Google Sheets by API, card-gated (2026-10-18 night).** The doer cannot drive the Sheets UI (tab clicks time
out, the Name Box appends), so cells are set over the Sheets API with the owner's own OAuth token instead. Pieces:
`agent/google.json` declares `extra_scopes: [spreadsheets]` on **`the owner-films` only** (`google_api.scopes_for`);
a token is loaded with the scopes it was *granted*, so the other accounts keep working and the owner adds the scope
with one re-consent: `python assistant/agent/google_api.py auth the owner-films` (`scopes the owner-films` shows granted
vs asked; `sheet <acct> <id> <range>` is the read-only CLI; there is no write command). `google_api.sheet_get`
reads (UNFORMATTED_VALUE: numbers stay numbers), `google_api.sheet_set` writes with **`valueInputOption=RAW`**
(a value is stored exactly as given, never parsed, so no formula can ever be created; a string starting with
`=` is refused outright). Exec kind **`sheet_set`** `{account, spreadsheet_id, tab?, cells[{a1, value, before}],
sheet_name?}` (lane `sheets`, not outbound, but a write, so card-gated): `cards.post` refuses a card unless
**every cell reference and value sits verbatim in the question** (`EXEC_VERBATIM`) and the account's token
already holds the Sheets scope (`SHEET_SCOPE_MISSING`, so an approval can never land on a write that cannot
run); `drain.py` re-reads every cell first and refuses the whole ticket, writing nothing, if one no longer holds
the `before` the owner saw, then writes each cell RAW, reads it back and reports `SHEET_DONE=n VERIFIED=yes`
under the card. Registry rows: `google-the owner-films` (mail + calendar + the write) and **`google-sheets-films`**
(`cmd_match` on `scopes the owner-films`: *not configured* until the consent, Setup button "Allow Sheets access").
**The pending edits** (no slow-season / weekday discounts, the Full Toasts & Speeches Film $500 row) live in
[`pricing/sheet_edits.json`](../pricing/sheet_edits.json) (business facts, never code); `scripts/sheet_fix.py`
reads each tab live, finds every cell by header + key, and posts ONE card (`Tab!A1: before -> after` per cell,
button **Write the cells**). Spine job **`sheet-fix`** (every 30 min, 07-22) runs it idempotently: waits for the
consent, never posts while a card is open or a ticket is queued, stops once the sheet holds every edit or the owner
tapped Skip (`data/sheet_fix_state.json`; a changed edits file is a new job). Nothing here answers a card.
Tests: `python assistant/tests/test_sheet_set.py` (stand-in Sheets API; the formula refusal, the verbatim
guard, the stale-before refusal, RAW writes with their types, the state machine).

**The Setup page (2026-10-12).** Mission Control `/setup` turns the registry into buttons: one row
per connection (plus every service the owners told Sage about in chat, `agent/org.py`), needs-attention
first, with the one step that connects it, a **Check**, and a screenshot of the agent's browser for
sign-ins started from the phone. Engine `scripts/setup_actions.py` (recipes from a row's optional
`setup` block or derived from its check type + renew text; never reads a secret, never sends), page
`scripts/setup_page.py`. Uploads from the chat paperclip land in the library (`agent/resources.py`,
`Raw/Uploads/`). A new organization starts from `assistant/new_org.py`. Details:
[agent/README.md](agent/README.md) "Learning any organization, the Setup page, and the library".
Tests: `python assistant/tests/test_org_setup.py`.

## Rescue kit (2026-09-30)

`assistant/scripts/rescue.ps1` mirrors the repo (full git history) and the git-ignored runtime
state (assistant/data, dashboard/data, scripts/projects, scripts/lib/.cache) to
`I:\VAULT_RESCUE` and `L:\VAULT_RESCUE` with a RESTORE.txt. Runs nightly from the Day Spine
(job `rescue-kit`). Local only: a private GitHub mirror is the owner's decision (`gh` is logged in
as owner; nothing is pushed until he says so).

## The doer - Sage's hands (2026-10-05)

`assistant/doer/` ([README](doer/README.md)). Sage is read-only; the operator drafts. Neither could *do*
anything on a website. The doer closes that gap without a standing lane: Sage's SUGGEST line gained
`do` (next to `task`), which queues ONE `agent_run` ticket and wakes `doer.py --tick` at once (spine
job `doer-tick` is the 10-minute backstop). The tick starts one headless model turn (config
`doer.model`, sonnet) with a private Playwright MCP on the one shared signed-in browser
(`browser.py`: Chromium on `~/.claude/pw-wv-cli-profile` with a CDP port, off-screen), works the task
under `doer/prompt.md` + `policy.json`, and exits. Zero tokens when nothing waits.

**Milestone notes + carrying on (2026-10-28, from the property OS's desk):** the turn writes a one-line note at each
milestone (`report.py note`, `doer/notes.py`); it is the job's checkpoint file, the live `Now:` line, and the first
four reach the job's chat thread as "still on it" lines (`receipts.note`). A run cut off by its step limit or time
limit is carried on from the notes (`doer.carry_on`, same session when there is one, up to `doer.continues_max`
more tries) before the owner hears it stopped, and then hears how far it got. Doer README "Milestone notes".

- **Three tiers.** Read or trivially reversible: done. Reversible write: done + screenshot. Irreversible,
  outward, money, identity, or any government / bank / payment / registrar site: fill everything, screenshot
  the review page, stop at a **checkpoint card** (`report.py`; the step sentence and every value verbatim on
  the card, EXEC_VERBATIM-enforced, the screenshot served at `/api/doer/shot/...`). The owner's Approve
  mints exec kind `agent_run` (lane `doer`); `drain.py` runs `doer.py --approved`, which resumes the same
  model session to perform exactly that step. A verification code the owner types in the note box is
  minted into the payload (never kept in `answers.jsonl`). Never: password changes, 2FA off, account
  deletion, trading, anything property-management.
- **Logins.** The profile's signed-in sessions cover most sites (`browser.py --show` to sign in once). The
  rest come from the agent's own KeePass vault (`vault.py`: `assistant/data/doer/agent.kdbx`, key in
  Credential Manager `kdbx:agent`; import from Google Password Manager's CSV, or add one at a time).
  `login_fill.py` types them into the page from a separate process; the model turn can only run that
  script, never read the vault. TOTP is automatic; SMS/email codes come back as a card.
- **Belt** (gate target `doer`, frozen in `gate_baseline.json`): explicit allow list, `--strict-mcp-config`,
  no mail / HoneyBook / web fetch / evaluate / file upload, Bash only for `login_fill.py`,
  `report.py checkpoint`, `vault.py has`; writes only under `assistant/out/doer/`. The tier call itself is
  the model following the prompt, so tier C is drawn wide and every run is logged (`~/.claude/doer-wv.log`).
- Connections `doer-vault` and `doer-browser` are in the registry; the Connections panel shows them.
  Tests: `python assistant/tests/test_doer.py` (vault, routing, cards, drain dry run, outcomes; no model).
  **Computer lane (2026-10-11):** `assistant/doer/computer.py` is the same doer with hands on the PC (a `do` with
  `where: computer`): Bash/Write/Edit under a code-enforced tier hook (`computer_policy.py`, `--settings`), checkpoint
  cards that carry the exact command (sha-bound, once), footage/credentials/system NEVER, self-built tools under
  `assistant/doer/tools/`. Tests: `python assistant/tests/test_computer.py`. Doc: [doer/README.md](doer/README.md).
  **Self-learning engine (2026-10-11):** `assistant/doer/learn.py` (zero-LLM) records every doer run, harvests the turn's
  `lesson.json` into a skill the same minute, scores tools, turns repeated hand-done jobs into 'build a tool' tickets and
  failed tools into repair tickets, and paces itself on the pressure of those opportunities (10 min when hot, 6 h when
  idle). Spine jobs `learn-tick`, `learn-digest`. Tests: `python assistant/tests/test_learn.py`.

## The thread comes from Chat itself (2026-10-29)

the owner, replying under the agent's 09:00 "Have the back taxes gone in yet?": "No you should know what I'm talking
about based on the thread I am replying to a message in / fix that context awareness." The agent had offered three
unrelated things, then said it "can't see the message at the top of a thread". Two faults, both in how the WHERE
block was built from the agent's own records: `place.py` took EVERY reply in a thread for a fresh top-level message
(it compared only the thread part of the message name, the bug `bridge._is_thread_root` had fixed on 10-16), so
THIS THREAD SO FAR was never built for a reply and the brain got the space's newest three lines instead; and the
record itself was incomplete (a cron's line was filed without its thread, a later line in a thread overwrote the
remembered root).

Now the conversation is read from Google Chat, not reconstructed:

- **`agent/chat_thread.py`**: `thread_lines(space, thread)` is one `spaces.messages.list` read filtered on the
  thread, with the reader sign-in (`space_reader`; app auth cannot filter by thread), every message oldest first,
  the local transcript's richer card text merged in; `space_tail(space, n)` is the space's newest messages across
  every thread. Cached per thread for the turn (`chat_threads.json`), refreshed whenever the thread has a message
  the cache does not hold. The first message is remembered as the thread's root.
- **`place.block`**: a reply in a thread gets THIS THREAD SO FAR "read from Google Chat itself, complete"; a
  one-message thread says "They are replying to this ONE message" and names it; a top-level message gets WHAT THEY
  ARE MOST LIKELY RESPONDING TO from Chat (replies inside threads marked). The transcript is the fallback when Chat
  cannot be read; the NOT ON RECORD line only when nothing anywhere has the thread.
- **The `chat_thread` tool** (gmcp.py; every turn kind, staff included, the turn's own place only): the whole
  thread, or the space's newest lines, when the block is not enough. The prompt: never "I can't see the top of a
  thread"; inside a thread only its own messages are candidates for "it" / "they" / "the ...".
- **Records**: `core.note_thread_root` is first-writer-wins; `chat_push.push_say` files a top-level line in the
  thread it starts.

Test: `tests/test_chat_thread.py` (the real 2026-10-29 thread as the fixture, a fake Chat; the live replay of the
09:44 message through `place.block` showed the back-taxes line as the thread's first message).

## Every input leads to its next action (2026-10-18) - the triage lanes, ported

the owner: *"Take a lesson from the triage lanes in property management and learn how every input leads to the
next logical action as opposed to our system that received example's contact info and did nothing with it."*
`scripts/triage_next.py` is that engine, zero-LLM, hooked into `tickets.add_many`: the moment an inbound
ticket lands (a text, an email, a HoneyBook inquiry) it is sorted. **Party** (whose couple / lead it is: the
ledger's phones and emails, the quiz lead files, the HoneyBook contacts) -> **facts** (a contact, "copy X
on everything", the questionnaire is done, they went another direction, a date or venue change; the words
live in `data/sop_map.json -> inputs.facts`) -> **actions** (`inputs.actions`, three gates: `auto` = code
does it now and records it: `also_notify` on the ledger entry, the contact on the couple's wedding page,
closing a questionnaire reminder, stopping a lost lead's follow-ups, an internal HoneyBook note; `card` =
code posts the exec card now (`hb_add_participant`, `hb_set_stage lost`), the owner's tap runs it; `operator` =
left for the drafter: the reply, a date/venue update). The ticket's `payload.triage` records all of it, the
operator prompt says to do only what is left, and Sage's prompt carries the rule. Replaying example's two
10-18 texts (`triage_next.py --dry --from +1555-031-6831 --prior "Here is example's number..." --text "And email: ..."`)
yields: participant card, couple page, also_notify, reply draft. Couple-page writes are queued and filed by
the spine (`triage-drain`, 10 min) because a wiki write lints and commits. Switch off: env `WV_NO_TRIAGE=1`.
Vocabulary and targets are data, so another business edits `sop_map.json`, never the code.
Tests: `tests/test_triage_next.py`.

### HoneyBook by connector, never by clicking (2026-10-18)

Four doer browser runs failed on HoneyBook's "Add" button while the connector could do it in one call. New
exec kinds in `hb_exec.py` / `exec_store.py` / `drain.py` / `cards.py`: **`hb_add_participant`**
{workspace_id|project_id, email, name?, phone?} (find-or-create the contact, add its user to the workspace,
read the member list back; HoneyBook mails the invite, so card-gated) and **`hb_set_stage`** {workspace_id|
project_id, stage, reason?} (a pipeline stage by NAME, read back; or `lost` / `archived` = archive with a
reason, since the owner's pipeline has no Lost stage; card-gated, a stage's automation may run). `hb_note` takes
`project_id` too (resolved through the desk snapshot). The weekly record check's lagging HoneyBook stages are
now **fixes** (`drift.py` op `hb_stage` -> `hb_set_stage`), not "[HoneyBook, your hand]"; targets in
`tracking.json -> drift.crm_stage.booked_stage / post_event_stage`.

**Preflight.** Emery's approved follow-up died twice on `comms.createCommunication is not a function`:
`hb_exec.py` pinned SDK 0.20.6, which had no such method (now 0.21.4). `hb_exec.py selftest` runs ONE
read-only headless check that every function each kind's template calls exists (`KIND_FUNCS`), writes
`data/hb_exec_status.json`, and says one line in chat when the shape changes; `cards.post` refuses an hb_*
card whose kind the self-test found dead (`HB_PATH_DEAD`), `hb_exec` refuses the run, and a live "is not a
function" marks its kind dead on the spot. Spine job `hb-send-selftest`, daily. Tests: `tests/test_hb_exec.py`.

### Couple notes, the builder's state, standing meeting guests, re-queues (2026-10-18)

- `agent/wiki_writer.py`: a Couples page takes notes inside a sync-safe **`## Story and notes`** region
  (`sage:notes` markers) that `hb_couples_sync.py` never touches; note type `couple`, `couple_notes(name, body)`,
  loose lookup (`find_couple("example and example")`). example & example's meeting recording is filed there.
  Tests: `tests/test_couple_notes.py`.
- `agent/builder.py`: `save_state()` merges with the file, so an owner's tap during a long run is never
  clobbered (09:34 approvals were overwritten by the 09:23 run's save). `scripts/builds_requeue.py` (spine
  `builds-requeue`) re-queues builds that failed on a regenerated-file "protected core" hit or on the account's
  session limit once the reset time has passed (the owner: "Build it another way", no builder.py edit for that).
- `scripts/meeting_guests.py` (spine `meeting-guests`, `config.json -> meetings.guests`): the standing guests
  (a team member) on every upcoming client video call that is also on the owner's Google calendar, by Calendar API with
  `sendUpdates=none` (they get the Meet on their calendar and join without knocking). A Meet that exists only
  inside HoneyBook is said once in chat: co-hosting there needs a HoneyBook team seat, the owner's call.
- The doer: `prompt.md` says HoneyBook and Google are connector-first (it stops and asks to be re-routed),
  a click that times out twice on "waiting for element to be stable" is forced once through `browser_evaluate`
  and then given up with the page state; `doer.py plain_error()` turns a max-turns / crash result into one
  plain sentence (no JSON in chat). Tests: `tests/test_doer.py`.

## sage-core: the engine, the pack, the supervisor, onboarding (2026-10-27)

the owner: "a copy of what Sage is and how it works but not org specific; remove any features that are specifically
niche to my org or system"; "no need for a HoneyBook connector module, that can be set by the user if desired."
The plan and its critique: [SAGE_CORE_PLAN_2026-10-27.md](SAGE_CORE_PLAN_2026-10-27.md). What is built, all tested end to
end on a fixture organization by `assistant/tests/test_sage_core.py` (37 checks: export, new_org from the export,
every command below, update, modules):

- **`engine.py`: the engine / pack contract.** ENGINE = the code and generic templates (the agent, deeds, cards,
  executor, doer, builder, dreaming, reflex, capability gaps, voice, outcomes, digest, spine, Mission Control,
  learnings, the wiki / semantic / dreaming / guardrails / monitoring / evals / skill-radar / drive-ingest tooling,
  Schema, the generic skills). PACK = the organization's own files (data, state, learnings, agent skills, profile /
  people / prompt / google / org / config / connections / tracking / chains / mining / spine_jobs, Wiki, Memory, Raw,
  Context, Schema/mistakes.md, task XMLs): an update never touches them. MODULES (`money`, `typeform`) ship with
  their jobs off. NICHE never ships: the wedding skills and templates, every HoneyBook script, the Resolve editor,
  card ingest, ads, lots, travel, taxes, rentals, shoot-day, the phone setup notes, planning history.
  `export <dst>` writes a clean `sage-core` (root path as `{{ORG_ROOT}}`, hosts / domains / owners scrubbed from docs
  and wrappers, never from code; `engine_manifest.json` with a sha per file; `--git` commits it). `update <org>
  --engine <core>` replaces engine files the organization did not touch, reports the ones it edited by hand (`--force`
  overwrites with a `.pre-update` copy), removes files the engine dropped, and skips every pack path.
- **`new_org.py` is the pack creator** (from this vault or from an exported engine): `install_tree` + the pack
  files, `org.json` with the machine keys the code used to hard-wire (`keyring_service`, `python`, `semantic_python`,
  `secrets_dir`, `google_home`, `lanes`, `lifecycle`, `modules`), `config.json` at maximum gating (shadow on, buttons
  off, texts held, cards to the owners' keys, no HoneyBook / meetings / projects / editor sections), the SOP map
  reduced to the generic inquiry follow-through on the business lane, module jobs and jobs whose script did not ship
  disabled with the reason on the row, the niche hook dropped from `.claude/settings.json`, generic Context / Memory /
  Schema/mistakes.md, the manifest. `alib`, `google_api`, `space_reader`, `gmcp` / `wiki_writer`, `hb_exec`, `drain`,
  `doer` read those keys (this organization's literals stay their fallback, so nothing changed here).
- **Hidden by default (engine rule, the owner 2026-10-28: "run processes in background and hidden by default unless the user
  needs to interact").** Nothing the engine starts opens a window: daemons run under `pythonw` (comms probe, relay
  poller, Mission Control, serve), scheduled tasks launch through the hidden launcher or `-WindowStyle Hidden`, every
  child process a runner starts carries `CREATE_NO_WINDOW` (`sage.py run_job`, `spine.py run_job`, the probes), and the
  doer's browser opens off-screen and comes on-screen only for a step a person must take. A new script that starts a
  process follows the same: `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)` on Windows, PowerShell with
  `-WindowStyle Hidden`, a browser `show=False`. The builder's rules say so; `test_sage_core.py` checks the runners.
- **`sage.py`: the one command.** `doctor` (Python, packages in the jobs' interpreter, Playwright browsers, git, node,
  gcloud, the Claude CLI, the token in User scope and no stray API key, a headless probe skipped inside a session
  with the reason, Mission Control, serve, secrets folder, Google, Chat, buttons mode, disk; `--json`, `--fix`),
  `serve` (ONE process: `assistant/serve.json` = spine, spine-long, probe, deadman, Mission Control keepalive,
  operator tick + morning, dreaming, habits, coverage; each on its cadence in its own thread, pause / stop files,
  heartbeat `data/serve_alive.json`; `--once --dry-run` for tests), `status`, `health` (did / failed / needs you,
  `--post` to Mission Control), `pause|resume|stop`, `install-startup|uninstall-startup` (schtasks UTF-16 at logon /
  launchd / systemd --user), `update`, `module on|off`, `new-org`, `export-engine`, `install-skill`, `uninstall`
  (stops, removes the startup entry, prints the revocation checklist, deletes nothing). Jobs run with `ASSISTANT_PY`
  and `ASSISTANT_CLAUDE` in their environment. This organization still runs on its scheduled tasks: cutting over to
  `serve` is the owner's call (the tasks and serve would double-run the same jobs).
- **`catalog.py` + `catalog/*.json`: the connector catalog** (24 tools; tier 1 connector / API, 2 CLI, 3 export /
  forwarding / webhook / device, 4 browser last resort; reads, writes, what ALWAYS waits for a card; `built` only
  where this code exists). HoneyBook is an entry (MCP connector, tier 1), not engine code.
- **`onboarding.py`: Discovery, Training, never-list, shadow week, go-live.** `discover` (one conversation over the
  14 business functions in `onboarding/discovery.json`; the catalog picks the tier), `map` (the capability map),
  `coverage` / `train` (the lifecycle skeleton for the business type, `onboarding/lifecycles/`: service studio,
  retail, trades, consultancy, rental; six fields per stage: trigger, actor, channel, template, clock, verifier;
  training stops at 100% coverage, never at a count), `never` (seeded at maximum gating into the learned org file),
  `shadow on|off` (config switches), `golive --enable <switch>` (standing exceptions on by name, dated),
  `blueprint` (a wiki note rendered from the answers, never the source). `--answers <file>` for every step.
- **`scripts/habits.py`: the habit ledger.** Ask classes from exec tickets (by kind) and doer runs (by the job's first
  words); verified counts from outcomes; a class with `habits.min_verified` verified runs and no script is `ready`;
  with `habits.codify = true` each becomes ONE build request through `capability.file_build`. Off here until the owner
  flips it; `report` shows the ledger.
- **Cards without buttons** (`config.json push.buttons = false`, `chat_push.buttons_on`): the choices are words,
  answered in the card's own thread, for a machine with no public HTTPS path for taps. This organization keeps buttons.
- **The `sage-setup` skill** (`assistant/setup_skill/sage-setup/`: SKILL.md, `bootstrap.ps1`, `bootstrap.sh`;
  installed at `~/.claude/skills/sage-setup` by `sage install-skill`): a co-pilot install from a bare machine. Claude
  installs what is missing (winget / brew / apt: git, Python, Node, gcloud; npm: the Claude CLI), makes the venv from
  `assistant/requirements.txt`, gets the engine (folder or git URL), creates the pack, runs doctor; the OWNER mints
  the Claude token (`claude setup-token`, User env var, never shipped with the skill), does every Google sign-in,
  consent and IAM grant; then serve, install-startup, Google in order, Discovery, Training, shadow week, go-live.
- **The owner-facing install and the auto-updater (2026-10-28).** the owner: "I want sage-core to have a super user-friendly
  set-up starting with the install ... installed like a typical program ... receive updates no matter what", an
  org-agnostic AI employee that is "friendly in any organization". Four pieces, `assistant/installer/README.md`:
  `release.py` (one zip of the engine's manifest files, no top folder, + `release.json` with version / date / sha256 /
  notes into git-ignored `dist/`; from an engine checkout as-is, from a pack exported first); `installer/windows/`
  (`SageSetup.iss` for Inno Setup + `install.ps1` + `build.ps1` -> `dist\SageSetup.exe`: per user, no admin, no git, no
  Node: the python.org embeddable 3.11 + pip + requirements under `%LOCALAPPDATA%\Sage\python`, the engine release
  under `engine\`, Playwright Chromium, the Claude CLI's native installer, a Start-menu entry, `install.json`);
  `welcome.py` (the setup page on 127.0.0.1:9018 and at `/welcome` on the local page: the three questions -> `new_org`,
  the doctor table with a fix line per FAIL, a button that opens the console for `sage token` (runs `claude setup-token`,
  the owner pastes the token hidden, stored as the User env var; never through the page), `sage probe` for the Check,
  the Google clicks with one link each, `sage install-startup` for Start; `pack.json` in `SAGE_HOME` names the pack);
  `updater.py` (nightly `serve.json` job `engine-update` 03:00-05:00 and `sage update` with no `--engine`: release.json
  at `org.json -> engine_release_url` / `SAGE_RELEASE_URL`, date-part compare (same date + other sha = "different",
  `--force` only), sha256-checked download, staged under `<pack>/.sage-engines/staging-<v>/`, gated there (compile,
  `doctor` against this pack's org.json, the staged `test_sage_core.py`), snapshot to `.sage-engines/previous/`,
  `engine.update`, `.sage_restart` so serve re-execs; `sage rollback`; a failure = one note on the local page, never
  Chat). The local page's brand and title come from `org.json` and the agent's name in `profile.json` (an organization may set
  `page_title`), so nothing owner-facing names this organization or that old title. Doctor: git WARN, Node info when the CLI is
  native, gcloud info, `claude cli` fix = the native installer line, a new `engine updates` row. Tests:
  `assistant/tests/test_installer.py`.
- **Zero personal data in sage-core (the owner, 2026-10-28: "The Sage-core should have zero personal data in it").** The export
  rules now scrub every line, code and prose: the organization's identity from `org.json` / `people.json` (owners ->
  `owner1`, staff -> `staff1`, names -> `example-org`, hosts -> `this-pc`, the vault path -> `{{ORG_ROOT}}`; prose reads
  "the owner", "the organization"), plus `engine.pii_scrub`: every e-mail address that is not a placeholder becomes a
  stable `p<hash>@example.invalid`, every non-555 phone number a stable `555-0xx-xxxx` of the same shape, and the words in
  `core_sync.json -> identity_terms` (places, clients, vendors, other organizations, product names) become `example`. A
  trailing comment on a code line is prose. `core_sync.py audit` flags the same set (identity + any address + any phone);
  `verify` runs it on every exported file; `refresh` (`zero_identity: true`) re-exports everything, REMOVES a file the rules
  cannot clean (held with the reason until a commit makes it clean) and removes what no longer ships (promoted to niche,
  pack DATA, a root file such as HANDOFF.md: `engine.ships` now needs an engine root, and only the named pack TEMPLATE
  files travel, never Wiki/ Memory/ data/ state/). `new_org.py` reads the source organization's words from the pack too
  (`_source_pairs`), so no name lives in code. Baseline "debt" is gone: 144 files with identity -> 0 after the refresh.
  The 13:51 session's identity-scrub work (engine `org_values` / `_build_scrubs` / `export_text`, core_sync `identity` /
  `verify` / `audit`) was finished and committed here: a module-level import it lacked, the manifest's `source`, and the
  trailing-comment rule.
- **`core_sync.py`: every change here flows on to sage-core, organization-free (2026-10-28).** the owner: "a program that
  automatically applies any changes to Sage to the sage-core org-agnostic system by making it not org specific, as
  long as it is not specifically a change that only applies to my business or life." `engine.py` draws the line by
  path and `export` writes the engine once; the sync is the continuous half. The spine job `core-sync` (every 30 min,
  zero LLM) walks every commit this folder made since the last sync and, per file: pack and niche paths never travel;
  a NEW file whose working lines are mostly about this business is **promoted to niche** on the spot (`core_sync.json
  -> niche_paths`, honoured by `engine.is_niche`, so `export` and `new_org` skip it too); prose is scrubbed with this
  organization's own names from `org.json` / `people.json` (hosts, domains, addresses, owners -> "the owner", staff ->
  "a team member"), and so are the comments and docstrings of code (`engine.export` leaves code alone); a **working
  line** of code that carries the organization's identity or its niche vocabulary (`core_sync.json -> niche_terms`:
  wedding, couple, HoneyBook, venue, Resolve ...) is a change that only applies to this business, so the file is
  **held** at its last clean version in sage-core and ONE build request is filed through the capability loop (the
  builder moves the code behind `org.json` / `config.json` / a module / a niche path); the next sync after that lands
  releases the hold by itself, and every generic change that waited with it lands at once. Lines already there before
  the first sync are **debt** (listed once, `debt`), never a hold, so the engine keeps moving while the old literals
  are worked off. Every exported `.py` must compile and no prose may still carry a host or address (`verify`), or the
  file is put back and held with the reason. One sage-core commit per vault commit, the vault's message scrubbed plus
  `(from <sha>)`, manifest and README refreshed. Record: `data/core_sync_state.json` (last synced commit, holds,
  debt), `data/core_sync_ledger.jsonl` (one row per file per commit); a Mission Control note per run that changed
  something, never Chat. `run [--dry-run] [--no-build]`, `status`, `holds`, `debt`, `ledger`, `classify <file>` (what
  the sync would do, line by line), `refresh` (re-export everything under the current rules after a scrub rule or
  `niche_terms` change). Switches: `assistant/core_sync.json` (pack: `core_root`, `niche_terms`,
  `niche_paths`, `allow_paths`, `allow_terms`, `niche_policy` / `identity_policy` hold|apply, `file_builds`,
  `notify`). The checkout is `C:\Users\the owner\Documents\sage-core` (bootstrapped by the first run); publishing it is
  a `git remote add` + push away. Tested on a fixture organization by `assistant/tests/test_core_sync.py`.
  **Two engines, one sync (the owner, same day: "make it do the same with example and example and example ... a system was already
  made to sync those, integrate that").** The property vault's one-shot `scripts/rescue/build_template.py` (drops
  property data, swaps example / example identifiers for `{{TOKENS}}`, scrubs residents / vendors / phones to
  neutral fakes; pushed once to GitHub `pm-operator-template` on 2026-09-19) is now the sync's second engine:
  `core_sync.py --engine template` (auto-detected from that file) IMPORTS the builder and uses its own drop, token
  and PII rules, with the template's contract kept (tokens over the whole file, code included; `instantiate.py`
  fills them). Holds there are what the builder's report called "not agnostic yet": hard-coded unit numbers, a
  bare property id, `niche_terms`. Build requests go through `scripts/builder_ops/request.py open`, board notes
  through `ask_card.py --note` (both `build_cmd` / `note_cmd` in `scripts/rescue/core_sync.json`); state under
  `scripts/rescue/.cache/core_sync/`. The boundary hook keeps this folder's sessions out of the property vault, so
  the property side runs the sage-core copy (`C:\Users\the owner\Documents\sage-core\assistant\core_sync.py --vault
  "<property vault>" run`) from its own day spine; the clone is `C:\Users\the owner\Documents\pm-operator-template`.
  Fixture test: `assistant/tests/test_core_sync_template.py`.
  **The relay between agents (the owner, same day: "I want upgrades to sync across all these agents as long as they are
  generally useful and not specific to the organization").** The two templates never copy files into each other (the
  codebases differ); what travels is the upgrade. When a sync lands a commit in its template whose applied files
  include a working script (not tests, docs, nightly state, skills or wiki notes), it drops ONE note in the commons,
  `C:\Users\the owner\Documents\agent-commons\upgrades\` (a neutral folder both boundary sides may use): frontmatter + the
  TEMPLATE's diff, so it is organization-free by construction. Every other agent's `run` reads the commons on each
  pass and files ONE port request with its own builder per note it has not seen (Sage: a wiki "Build request Port
  from <label> - <subject>" with the diff inline and the rule "if this agent already does this or has no subsystem it
  fits, answer not_code"; the property side: `build_cmd` with the diff written under its state folder). Capped at
  `relay.max_per_run` (3) per run, 12 KB of diff per note; the first inbox pass skips what was already there
  (`relay.backfill` N keeps the last N); a note is filed once (`relay_seen` in the state). Switch:
  `core_sync.json -> relay` on each side. Fixture test: `assistant/tests/test_core_sync_relay.py` (both directions).
  The one-off inventory of what sage-core lacks today is a separate scout (`PORT_SCOUT_2026-10-28.md` when it lands).
- **The organization's extensions by name: `alib.plugins()` (2026-10-28).** The first pass over the debt moved every
  working line that named this organization or its niche out of the runtime engine files (`exec_store`, `drain`,
  `client_texts`, `chat_push`, `brain`, `sentinels`, `phone_bridge`, `doer`, `space_reader`). Identity now comes from
  the pack: `people.json` (the approver `exec_store.APPROVER` = the first owner's key, the blanks regex, card and note
  defaults, lane names, the "<Owner> asked:" prefix, requester names), `org.json` (`chat_reader`, `phone_nudge_mark`,
  `phone_token_header`, with `send_identities` / `phone_accounts` / `watched_tasks` / `mission_control_url` no longer
  carrying this organization's literals as fallbacks: no org.json = nothing sends, nothing watched) and
  `config.json -> lanes[0]` (`alib.business_lane()`, the business lane). Niche code sits behind
  **`config.json -> plugins.modules`**: modules under `assistant/scripts` the engine asks for hooks by attribute
  (`alib.plugin_map / plugin_set / plugin_call / plugin_each`) and runs the same without. This organization's two are
  niche paths (`core_sync.json -> niche_paths`, never exported): `crm_honeybook.py` (the HoneyBook ticket kinds,
  validators, executors, result lines, after-done follow-ups, stale-write reason, card button words, the agent turn's
  MCP server, sentinel findings, the doer's standing Publish & share step) and `texts_honeybook.py` (who a standing text
  goes to: lead files, the snapshot's contacts, project_state; the questionnaire file, meeting reminders, the Finishing
  Touches file). `text_gate` speaks of the business party now (`business_only / business_numbers / is_business`), the
  doer's sign-in window is `config.json -> doer.signin_window`, and `the owner_request` is an `allow_terms` identifier (a
  ticket kind the code shares, as `test_new_org_boot` already says). Left as they are, on purpose: the allow / deny lists
  in `brain.py` and `doer.py` that `check_gates.py` freezes (six lines naming the HoneyBook connector tools).
- **Card taps without Tailscale (2026-10-28).** The Apps Script relay (`agent/gchat_relay/Code.gs`) has a **pull
  mode**: events queue in the script's cache, `scripts/relay_poll.py` (serve job `relay-poll`) fetches them through the
  relay's web app URL every few seconds, hands each to Mission Control's `/gchat/events` on loopback and posts the
  reply back inside Chat's wait; late ones ride `X-Sage-Late` and finish by REST. `sage relay secret|url|check|poll`;
  `check` turns `push.buttons` on. Mission Control's port is `org.json mission_control_port` now (two packs on one PC).
  Tests: `assistant/tests/test_relay.py` (27 checks: the poller against a fake relay and a fake Mission Control, vault
  chat, the dialog, a real KeePass write, contacts, the script's pull mode).
- **Logins from chat, never in chat (2026-10-28).** `scripts/vault_chat.py`: an owner's "save my Stripe login" (or a
  pasted password, refused and scrubbed from every agent record by code before any model turn) gets one card whose
  **Enter login** button opens a Chat dialog (`chat_push.vault_dialog`: site, username, password, code-generator
  secret); the submit (`gchat_events.vault_click`) hands the fields to `doer/vault.py add-stdin` in the keyring
  interpreter and closes with "nothing was kept in chat". The relay never holds a vault submit. Bridge hook:
  `_vault_offer`, before `connect`.
- **Contacts from chat (2026-10-28).** A `.vcf` shared into the DM is imported for the owner who sent it
  (`contacts.import_file`, owners from `people.json`), the downloaded copy deleted, one line back (`bridge._import_contacts`).
- **The wiki stack runs on its own in a new organization (2026-10-29).** A read-only scout (2026-10-28) judged the wiki side
  "adequate core, weak for running on its own": the contract, the nightly loop and onboarding were all this organization's.
  Now: the engine ships a **generic schema** under `assistant/templates/Schema/` (types person / client / project / vendor /
  provider / asset / account / concept / reference / workflow / integration / builder, a short tag list, a generic naming
  rule, and a machine-read ```` ```routing ```` block for the folders `new_org.py` creates: People, Clients, Operations,
  Vendors, Integrations, Builder, the lane folders) with generic about pages and person / client / vendor / wiki-note /
  daily-log templates; `new_org.py` installs THAT into the pack's `Schema/`, and this vault's own `Schema/schema.md`,
  `about.md`, `templates/` and `skills/` are niche + pack (never exported, never updated). `wiki_writer.py` reads its
  types from the schema's ```types block, its folders from the ```routing block (else this organization's defaults),
  the default lane from `config.json -> lanes`, and lints ONLY the note it wrote (`wiki_tool.py lint <path>`; the
  rollback on a failed lint stays), so one older note that fails the contract no longer refuses every chat note.
  `onboarding.py blueprint` writes the full front matter (it used to fail lint and block every write), and `discover`
  ends with the `wiki_tool.py sources` reminder. The nightly loop is portable: `scripts/dream_cron.ps1` finds its folder
  from its own path and the Claude CLI from `ASSISTANT_CLAUDE` or `sage.py claude-cli` (new command; the same lookup
  `claude_cli()` does), runs only the test suites that exist and the CRM step only when its script exists;
  `scripts/graphify_cron.ps1` reads its python from `org.json` (`semantic_python` / `python`, then plain `python`);
  `scripts/drive_ingest/` is niche (this organization's Drive accounts); task XMLs ship with `{{USER_SID}}` in place of
  this PC's account SID (`engine.pii_scrub`; `new_org.py` writes the installing user's; `core_sync audit` flags a SID);
  `wiki_tool.py hook-pending` promises the nightly ingest only when a dreaming job is actually wired (`dreaming_wired`:
  serve heartbeat + job, a fresh dreaming log, or a dreaming task in `org.json -> watched_tasks`), else it says the
  backlog does not drain and how to ingest; `sage doctor` has a "semantic search" row and `requirements-semantic.txt`
  (fastembed + numpy) is the documented optional extra; `semantic.py` without them prints one line and exits 2 (the hook
  stays silent). First-run path for an organization's own documents: `sage-setup` SKILL.md section 5b (put them in
  `Raw/`, run `wiki_tool.py sources`, say "ingest"; nothing automatic). Tests: `assistant/tests/test_wiki_tooling.py`
  (wiki_tool on a temp vault from the generic schema, session_capture, the semantic fallback) and new checks in
  `test_sage_core.py` (generic schema in the pack, lint 0 after the blueprint and after `wiki_writer.write`, no SID in
  the engine). Left as they were: `semantic.py AREA_BOOST` (its folder penalties are harmless where the folders do not
  exist), the operator / dreaming wrappers being PowerShell.
- **The wiki schema grows with the organization (2026-10-29).** `agent/schema_growth.py` is the one path by which the
  schema's fenced blocks change on their own, in three seams. **Seed:** each lifecycle skeleton has a `wiki` section
  (a page type per core noun with the owner's likely words for it, tags from the stage names, a folder per type) and
  Discovery asks what the business keeps track of (`discovery.json -> tracks`); `onboarding.py discover` / `train` (and
  `seed-wiki`) write the types, tags, `type x: Folder` routing lines and `Wiki/<Folder>/about.md` once, idempotently; the
  blueprint links the folders. **Gaps:** `wiki_writer.write` asked for a type the schema lacks records a gap
  (`data/agent/schema_gaps.jsonl`: name, lane, title, who, when, the asker's words) and returns the refusal with the
  nearest allowed type (`suggest`), which the chat turn and the dream re-file under; an unknown tag is dropped and
  recorded; `wiki_writer.py gaps` lists them. **Growth:** the dream's `schema_step` (after the rule merge) adds a gap
  asked for by two notes, or once by an owner in so many words ("make a page type for X"), as one line (+ routing,
  folder, about.md), at most 3 a night. Rails: only adds, never renames or removes; plain lowercase names; never the
  protected core; `wiki_tool.py lint` before and after, rolled back when errors rise; ledgered (`schema_remove`) and in
  the morning note; an undo is latched (never re-added) and refused while a note still uses the name; a schema without
  a routing block (this organization's own) gets the type line only, never a routing block. Switch:
  `config.json -> wiki.schema_growth`. `wiki_tool.py` also resolves path links (`[[Wiki/X/about|X]]`). Test:
  `tests/test_schema_growth.py`.
- **Not done yet:** a published `sage-core` repository (the checkout exists; add a remote and push it); the Chat-tap relay is built (pull mode above) but not yet deployed for a second organization; a Chat-tap relay that
  needs no Tailscale (buttons stay off for a new organization until then); the operator and dreaming wrappers are
  PowerShell (`windows_only` in serve.json), so macOS / Linux run everything but those; the daily three-line health
  report in Sage's own voice (today `sage health` prints it and `--post` puts it on Mission Control); cutting this
  organization over from its scheduled tasks to `serve`; a 7-day unattended run of the fixture on a clean VM.

## Not built yet

- The doer's first live run (the nested CLI cannot authenticate inside a session; everything but the model
  turn is tested).
- LLM execution-verifier - deferred until a real write action exists (today the only
  write is the draft append, which is verified deterministically).
- Calendar: exotic RRULEs (BYMONTHDAY, BYSETPOS, hourly...) show on their first date
  only. DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL/COUNT/UNTIL/BYDAY/EXDATE are expanded.

## Also wired in

- **Mission control** (`dashboard.py`): one brand-styled static page - needs-you list,
  latest brief, next 14 days, bookings, queue by lane, system health (sensors, creds,
  HoneyBook session, task registration, last tick, budget), notifications, log tail.
  Read-only, zero LLM. The morning run adds `--hb-check` (HoneyBook session probe).

- `python scripts/open_loops.py` (the "what's open" roll-up) lists the queue: staged
  tickets under BLOCKED (your move), new/claimed under WAITING.
- The morning run prunes closed tickets older than 30 days.

Tests: `python assistant/tests/test_assistant.py` - queue, calendar recurrence, and the
mail sensor + draft path against a fake IMAP server (lane routing, skip-senders, bulk
flag, seen-tracking, draft goes only to the original sender and is verified in Drafts).
