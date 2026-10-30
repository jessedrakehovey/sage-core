# Sage - the household agent (assistant/agent)

Built 2026-10-05. The agent the owner and owner talk to. Modelled on the property OS's example
and example (a chat bridge, a read-only brain, a SUGGEST line, learned rules, a nightly dream),
with three powers they do not have: it writes wiki notes, creates its own schedules, and
adds events to the owners' calendars. It learns from its own conversations, so nobody has
to open Claude Code for it to improve.

Both owners are equal (`people.json`). Nobody else is ever answered.

## Projects: long-running problems worked on a schedule, with every try on record (2026-10-27, evening)

the owner, 2026-10-27: "example has long term problem solving capabilities under projects. I want Sage to manage long term issues
and problem solve the same way." The reflex is minutes-to-an-hour faults in the machinery; projects are days-to-months
problems in the owners' life and business. The shape is example's case file (a goal, a done test, routes tried in order with a
clock on each, escalate once when exhausted), in Sage's own pieces: `agent/projects.py`, zero-LLM, one record per problem in
`data/agent/projects.json`. What 10-25/26 looked like without it: the Elantra got eleven doer jobs in one afternoon and
nothing on record said which approaches were dead; the back-tax upload was re-queued into the same wall twice.

- **The record**: title, goal, lane, status (open / waiting / done / parked), hypothesis, `tried` (every step with its
  outcome), `next` (what, kind, due, an optional `then`), `approaches` (untried fallbacks in order), what counts as solved,
  the owners' notes, the thread it lives in. Every change is a ledger row with an undo (`core.undo -> project_restore`).
- **The deed** `project` (gmcp -> `intents._validate_project` -> `actions.apply` -> `projects.apply_deed`): `open` the turn
  an owner describes something recurring or stuck for days ("the Elantra registration keeps failing", "Sloane's film has
  been stuck in review for weeks", "our ad leads go quiet"); `update` when they add a fact, correct the thinking, name a way
  to try, or after a step ends; `park` / `close` / `resume`. A duplicate title folds. Owners only. A doer/task step that pays,
  moves money, deletes or handles a credential is refused (kind owner is theirs). "Where are we on X" is answered from the
  **PROJECTS** block in the brain's context (beside BUILDS and REFLEX), never "I don't know where that stands".
- **The pass** (`projects.py tick`, spine job `projects` every 30 min): a project whose next step is due is run through the
  existing hands by kind: `doer` (an agent_run ticket carrying the case, in the project's thread), `task` (an operator ticket
  -> a card for the tap), `build` (ONE build request carrying the case, the builder kicked), `owner` (ONE plain line through
  `cards.say`; the voice gate applies; inside the day window), `decide` (ONE card with 2-3 buttons, not urgent: it rides the
  weekly digest and is open on Mission Control), `wait` (check back at `due`). Outcomes are read back from `tickets.json`
  (done / blocked / dismissed / a limit), `builds.json`, the cards' answers, the owner's next word on the project.
- **The clock**: a hand past its wait fails the step (doer 4 h, a doer at a checkpoint card 24 h, task 72 h, build 36 h,
  owner 72 h, decide 8 d). A failed step moves to the NEXT untried approach, never the same one again (`_pop_approach`
  skips anything already tried and failed). A step the owner was asked for is asked once; their next word on the project
  settles it.
- **Planning**: a step that ends with nothing after it (no `then`, no approach on a success) sets `needs_plan`; the bridge's
  minute tick (`projects.tick_plans`) hands ONE short brain turn the case (PLAN_NOTE: set the next step with the tool, reply
  only if the owner should hear it, else NO_REPLY), capped at 3 per project and 12 a day. No turn within PLAN_GRACE_MIN
  (bridge down, caps hit: PLAN_FALLBACK_H) -> code takes the next approach itself. Out of approaches and nothing set ->
  **escalate once**: one plain line ("I've run out of ways on X. I tried A, B and C. What would move it: ...") and the
  project waits on the owners; never a second line while they are silent; their next word reopens it.
- **Reading it**: `/projects` in chat; `python assistant/agent/projects.py list | show p1 | status | context | tick --dry-run`;
  the Monday scorecard's "N long-running problems on my board, M moved this week, K solved"; the CLI `open / tried / set /
  park / close / resume` for a session.
- **Seeded live**: p1 the Elantra registration and title (8 tries from 10-25/26 on record; waiting on the owner for Brian's
  signed title since 10-25 17:45, then a text-to-Brian card, the eTags reship from the mailbox that can send, the DMV
  appointment and fee lookup); p2 the back-tax packet into Drive (2 tries; next a decision card: copy it from this PC or
  he uploads it from his phone).

Tests: `tests/test_projects.py` (56 checks, scratch stores). Generic: the module names no organization, person or service
(the test checks its code lines); `templates/prompt.generic.md` carries the same deed line as `prompt.md`.

## The reflex: it notices it is failing and changes course by code, within the hour (2026-10-27)

the owner, 2026-10-27: "Sage was unable to do most of what I asked it today in both google spaces. I need sage to be able to
recognize when it is failing or should be adapting and change quickly ... recognize a failure and correct itself nearly
100% within the hour." What 2026-10-26 was, from the stores: the card sweep timed out 56 runs in a row (every sweep
re-ran the sentinel checks once per open alarm card: 22 cards x 6.6 s) and nothing changed course; "no more of these
messages" (11:51, 19:37) became model rules while the "Still not built" and "Not confirmed: build" lines kept coming
from code; a usage limit held his questions 50 minutes with the 20x account idle and the doer retried twice into the
same limit; builds that conflicted with a moved master, or were done by hand, were rebuilt every run and never
landed; "the card is below" was said about a card the draft lint had refused. Every one had a signal in a file this
system already writes. `agent/reflex.py` reads them, zero-LLM, on the spine every 10 min (`reflex` job) and on the
bridge's minute tick (the limit detectors only):

- **detect**: `job:<name>` (a spine job failing SPINE_FAILS runs in a row), `limit:held` (questions held on a usage
  limit while an account has room), `limit:doer:<ticket>` (a doer job staged on a limit whose reset passed),
  `build:<key>` (built, not landed: master moved under it), `gap:<id>` (an auto-filed build request nobody picked up
  in 6 h), `leak:<class>` (a line an owner switched off, posted anyway).
- **fix, one step a pass, cheapest first**: run the job alone with a `faulthandler` stack dump at its budget (clean
  under budget -> the pass was crowded, counter reset; clean over budget -> `job_overrides` raises its budget, the
  spine reads it; still failing -> ONE build request carrying the stack); release the held questions (the bridge
  re-runs them that minute); re-queue the doer job and close its "try again" duplicate; `builder.land(key)`
  (cherry-pick the commit on today's master, py_compile + the touched tests, fast-forward; a conflict is counted,
  never an endless rebuild); `builder.kick(force)` on the stalled request (one start a pass, three a day); a leak files
  one build request naming the gate.
- **verify**: a finding has VERIFY_MIN (60) to clear; the detector no longer reporting it = fixed, minutes kept.
  Still there -> the next step; out of steps and still there an hour later -> escalated ONCE: a Mission Control note,
  and a chat line only when the owner has to do something (r100: restart quietly, speak when it stays broken).
- **mute**: `CLASSES` names the code-posted lines an owner can switch off with a sentence (`build_slipped`,
  `build_landed`, `build_failed`, `build_unconfirmed`, `text_unconfirmed`, `scorecard`, `review_summary`,
  `system_status`, `chase_line`). `core.rule_add` -> `reflex.bind_rule` mutes the classes the rule names the same
  turn (ledgered `reflex_mute`, undoable); `bind_rules()` on every tick binds the rules written before the module
  existed (r36, r99, r100, r110, r116 -> seven classes on 2026-10-27). Posters ask `reflex.allowed(cls)` first:
  `commitments.line_for`, `outcomes.ask_owner`, `dream` (review summary), `deadman` ("is back", stdlib), the sentinels'
  chase line. A muted class that posts anyway is a `leak` finding. Transcript-only lines (the builder's "I built that thing" note, channel `builder`) are never posted, so they are no leak.
- **accounts**: `account.order()` puts the account known to have room first; every runner (brain, doer, builder)
  walks it once and `note_limit`s a refused account until the reset it names (`data/agent/limits.json`), so no later
  turn pays for a refused call. `held.line` says "both of my accounts are at their limit" when that is the truth.
- **the reply**: `reflex.fix_reply` replaces "the card is below" with the truth when the card was refused this turn.
  `cards.reopen` refuses the SAME failure twice (`SAME_FAILURE`); drain says the portal line once instead.
- **the sweep**: `sentinels.collect()` is memoised 120 s inside a process and the scheduled-task query is cached ten
  minutes (`task_status_cache.json`); a `job:` the reflex has in hand is not carded to the owner.
- **reading it**: `/reflex` in chat, `python assistant/agent/reflex.py status | tick | stats | muted | replay <day>`;
  the REFLEX block in the brain's context (never "I can't switch accounts / fix the job from here"); the Monday
  scorecard's "N faults I caught myself, M fixed within the hour". `builder.py land [key]` and
  `builder.py mark <key> "what"` (a request done by hand is never rebuilt; a note saying "Built by hand" either).
- **new spaces** (the owner, same day: "work instantly in any google space I add it to"): `gchat.record_added` handles a
  SPACE add: the space list cache is dropped, the space's watermark starts at the add, members are fetched for @-tags,
  one greeting line (an owner's add), and the space reader reads a first-seen space 30 min back
  (`NEW_SPACE_WINDOW_S`). A non-home space is business-only from its first line (`core.scope_of`).

Tests: `tests/test_reflex.py` (88 checks, scratch stores). Shadow period: `python assistant/agent/reflex.py status`
for three days; the first live replay of a stalled gap through the builder; the first `leak` (none expected).

## The voice: one door to Chat, and only a person's words go through it (2026-10-27, afternoon)

the owner, the same day the reflex landed: "Sage still sends me lots of system noise I don't want to see. Interacting
with Sage should feel exactly like interacting with a real person." The three days before, from the transcript: 81
cards and 128 code-posted lines reached the home space, about half of them the machinery talking ("Heads up: the
comms probe has stopped", "Drain ran: DRAIN ran=0 done=0", "Still waiting on you: card open 39h unanswered", "Still
not built: ...", "Not done: NOT_CONFIGURED smtp:personal (set with setup_creds.py ...)", "Text to +15550734973 handed
to the phone", the same "I built a fix" in two threads a minute apart, cards headed "Not confirmed: agent run",
"A nightly self-test failed", "Spine job cards-sweep failing"). Every poster had its own words and its own idea of
urgency, and the weekly-digest rule was beaten by each poster's own `"urgent": True`.

- **`voice.py` is the one gate.** `chat_push.push_say / push_result / push_text`, `receipts.say` (the doer's and the
  builder's lines), and `bridge.post` (routines) all pass their text through `voice.gate(text, channel, space)`.
  System talk never reaches Chat: status lines, raw job output, chase lines, scorecards, review summaries, slipped
  builds, unconfirmed outcomes, the morning digest (their Mission Control note is written before the gate, so nothing
  is lost; `voice.recent()` counts what was held, shown by `/reflex`). Lines a person would say are reworded: "Texted
  Quinn Heathcote at 12:19 PM, from your phone"; "Emailed Corwin Castellan IV at 4:45 PM" (the Message-ID stays on the
  card note); "Done: approval cards now show an hourglass ..." for a build receipt (a receipt whose "what changed" is
  a truncated title says "Done, that's built: ..."); "I need the 6-digit MyDMV code ... Reply with it here" for a doer
  waiting on a code; a blocked doer line quotes the job cut at a word; "On it." for a tap ack; a routine drops its
  "Title: " prefix. Names for numbers everywhere (`voice.names`, from the contacts; an unknown number reads
  "(805) 555-0123"), ids, ticket codes, preview markers out of every line (`voice.polish`, the model's reply
  included). The same words to the same place within an hour post once (`voice.fresh`). Switches:
  `config.json -> push.voice {system_lines, dedupe_minutes}`.
- **A card's urgency is what the card IS.** `cards.is_system`: a sentinel finding, an outcome the ledger could not
  confirm, a connection down, a channel paused, a failed self-test, a spine job failing (by key or by header) is
  NEVER urgent whatever its poster said, never in the digest: delivery `mc`, open on Mission Control and in the
  transcript (marked `held`, so a muted class there is no leak). The owner's own decisions are untouched: a send or
  reply (urgent where asked), "Build needs your OK", a stalled ask ("Waiting Nd"), a lead's next step. Switch:
  `cards.system_to_chat` (env `ASSISTANT_CARDS_SYSTEM=chat` in tests).
- **The reflex works the system cards** (`reflex.d_system_cards` + `fix_system_card`): step 0 presses the card's own
  recommended button by code (run the job / the task / the drain / the sensor), an hour later a still-open finding
  becomes ONE build request with the evidence, and only a sign-in that is the owner's to do (a connection card) or a
  paused channel with approved items behind it is said in Chat, in the card's own words, once a week at most. Nothing
  else about the machinery reaches an owner.
- **The prompt** carries the same rule for Sage's own replies: never an id, a code, a path, a module or job name, a
  raw error unless they need it to open something; people by name.

Tests: `test_voice.py` (59). The two build requests this closes by hand are marked (`builder.py mark`): "Handed-to-phone
lines name the person" and "How about instead of making me confirm everything you just check".

## "Stop sending me messages like this": switched off by code, in the thread, model or no model (2026-10-27, evening)

the owner, 2026-10-27 evening: "Sage failed to answer a few things and my request for it to stop sending certain messages
by replying to them in the thread is not working. Sage should be capable of taking my requests and changing so I don't
see messages I ask not to see." What the stores showed: at 16:55 he replied "Stop sending me messages like this" under a
"Dropped: imessage send to Quinn Heathcote (nobody could confirm it)" line, with a plant photo and a question in the same
message; at 16:58 "Try again"; at 17:52 a voice note. All three turns were refused by the max5x account ("Your
organization has disabled Claude subscription access for Claude Code"), which is not a usage limit, so the code called
it a one-off ("That one didn't go through on my side"), never tried max20x, kept nothing, and the stop request never
became anything. Two fixes, both zero-LLM:

- **The stop request is handled by code before any model turn** (`bridge.handle` -> `reflex.mute_from_thread`). A
  tightening sentence that points at *this* ("stop sending me messages like this", "no more of these", "don't send me
  stuff like this") takes its class from the thread: the last line the code or a card posted there (the bridge's own
  "didn't go through" / "out of steam" lines are skipped). A known class (`reflex.CLASSES`, now with `dropped` and
  `unconfirmed_ask`) is muted; a line of no known class gets a **custom class** made from its opening words (a card's
  header, or the first words up to a number), kept in `reflex.json -> custom`, so every later line with that opening
  is muted too; the model's own words get a rule instead (no code posts them). A sentence that names a class works
  anywhere ("stop sending me 'still waiting on you' chase lines"). The owner hears one line ("Done. No more 'dropped,
  nobody could confirm it' notes from me."); whatever else the message asks goes on to the brain with a note that this
  part is done, so it says so in a few words and does not ask which messages. Every mute is a ledger row (undoable).
- **Every mute is enforced at the voice gate** (`voice.gate` asks `reflex.line_class` + `reflex.allowed`), so a muted
  class is held at the one door whatever code posts it; the per-poster `allowed()` checks stay as a first line, and
  `d_leaks` only sees what got past both. `Dropped: ... (nobody could confirm it)` is also in `voice.BLOCKED` outright:
  a handed text is sent in practice (the phone's Shortcut never acks), so the line told him nothing he could act on.
- **An account that refuses a turn is handled like one at its limit** (`account.is_refusal` / `out_until`): the turn
  runs on the next account the same minute (brain, doer, builder all walk `account.order()`); both out -> the question
  is kept (`held.hold(..., reason="refused")`) with an honest line ("I can't reach my model right now; I've kept your
  question and will answer it the minute I'm back") and re-run when the account is back. The reflex's `account:<name>`
  finding probes a refused account with one cheap haiku turn an hour (`account.probe`), clears its note the moment it
  answers (the held questions release through `limit:held`), and says one line to the owner only when *both* accounts
  are out, since one account carrying the turns needs nothing from him.

Tests: `tests/test_reflex.py` section 10 replays the real thread (the Dropped line, the two failure lines, the stop
request with the plant question) through `bridge.handle` with both accounts refusing: the switch-off is said, no model
turn is spent on a stop request alone, the second account is tried the same turn, the question is held with its reason,
the probe clears the account that answers, and the owner's line comes only when both are out.

## A correction is the ask again: "that's not what I asked" / "where is the card" / "try again" (2026-10-27, night)

the owner: "When I have an interaction with Sage where I ask it to do something and it replies and I respond no that's not
what I asked or where is the approval card or whatever it is I follow up with, Sage needs to fix whatever it was. It
cannot just respond, say sorry, and never change." The week's transcript: "You told me the card is below, but you never
sent one" got "You're right, and I'm sorry ... I can't see why" and the card 90 minutes later; "Why is it not built?"
got "I can't see why from here"; "Try again" got "Sorry, I told you the card was below and I can't back that up". The
model took each correction as conversation. `assistant/agent/correction.py`, zero-LLM, around the unchanged brain:

- **Detect** (`correction.kind`): `card` (where is the card, no card, it never came / posted), `redo` (not what I asked,
  try again, do it again, that's wrong), `build` (why is it not built, not working), `fix` (fix it so that doesn't
  happen again). A factual "No, Diane texted us" is none of these: its deed is `learn` / `remember`, nothing is forced.
  A correction is never dropped as a repeat (two "Try again" in fifteen minutes are two corrections).
- **The ask** (`original_ask`): the owner's last real ask before the correction, in this thread first (a card's
  thread, the DM), then the conversation; taps, acks and other corrections are skipped.
- **The turn**: the brain gets a CORRECTION block carrying the ask and what is expected (`expected_deed`: a message ->
  `task`, work somewhere -> `do`, else whatever the ask implied). The prompt's rule: do it this turn, with the deed,
  "sorry" is three words at most, never "I can't see why". Then by code: an apology with nothing put forward (kind
  card / redo / fix) is **re-run once** with the apology named as what not to do and the second reply replaces the
  first; the deed the ask implied is **queued from the original ask's words** when the turn still has none (a task,
  a doer job; "Again:" in front so the ticket is not a duplicate of the first try) and the reply gets the lead line
  ("I'm getting that drafted..."). Nothing is ever forced for a question or a fact.
- **The card is already there** (`card_in_thread`, 2026-10-18 the Hollis text): "where is the card" in a thread a card
  landed in since the ask (open, or tapped) is pointed at, never made again (one that expired untapped is made again); the brain gets `here_note` with
  how it stands (`place._card_state`: open / tapped and sent), nothing is forced or re-run, and the ledger row's
  `card_here` keeps the reflex from filing a build. A closed card's own thread context says how it closed too, so a
  tapped Send is never described as "closed unsent" and an owner's message there never changes it.
- **Ledger** `data/agent/corrections.jsonl` (`python assistant/agent/correction.py [hours]`): kind, the ask, what was
  expected, what the turn put forward, what code forced, whether a re-run was needed, `words_only`. The reflex's
  `correction:<ts>` finding (`d_corrections`) takes a correction that still ended in words alone and files ONE build
  request a day per kind with the pair (the ask, the correction, the reply), so the gap is fixed in code, not in chat.
  A turn whose reply said it could not and filed its capability build (`correction.handed_on`) is not words alone.

Tests: `tests/test_correction.py` (58): the real lines classified, the thread-first ask, the apology re-run with the
CORRECTION block, the forced task and the forced doer job, a fact correction left alone, the card already in the
thread (open, then tapped), the reflex build request.

## The capability loop: it builds what it cannot do, then finishes the ask (2026-10-25)

the owner: "When I tell it to do something new it can't do yet it should rapidly adapt to fulfil that request ...
if there are any roadblocks they should be solvable in chat. I should never have to open Claude and say 'Sage
couldn't do this, fix it' like I have been doing all week ... in such a way it can be shared with different
organizations." All week that loop was a human one (10-23, 10-24, 10-25 below: a coding session read the
transcript, found the "can't" lines, built the fixes). `agent/capability.py` is that loop as code, zero-LLM,
around the same read-only brain:

- **A gap is found at three seams.** (1) The brain's own reply, right after `actions.apply`: a "can't / outside
  what I may do / nothing like that exists / no way for me to" with no deed behind it (`capability.after_turn`,
  called by the bridge). (2) A doer report that refused on policy ("outside what I may do", "route it through",
  "no tool"), read from the transcript (`capability.scan`, bridge minute tick). (3) An owner's complaint right
  after a reply ("you fail again", "it was never sent", "just fix it", "still isn't", "I don't want to see
  messages like this"): a **regression** gap whose ask is the owner's ORIGINAL line before that reply. A line
  whose blocker is the owner's own (a sign-in, a password, a fact only they hold, a tap, a connection that is
  down) is NOT a gap: that is rung 6, the portal, said once. `classify()` decides; the regexes are narrow.
  Nor is an owner's question about a build ("Why is it not built?", "is it done?", `about_a_build()`, 2026-10-25):
  it is answered from BUILDS, never filed as a build, and gets no "building it now" line, at any of the seams.
- **Every gap files ONE build request at once**, the same `## Build request` wiki note an owner files in chat,
  carrying the ask verbatim, what was answered, what to build and an **Acceptance** line ("when the ask above
  is replayed, it completes with a deed"); de-duplicated against open gaps (7 days, same ask by content words)
  and open build notes; capped at `FILED_PER_DAY` (8); `builder.kick(force=True, first=...)` starts it that
  minute. The reply gets one sentence ("That piece isn't something I can do yet, so I'm building it now; I'll
  pick this up myself here once it's in"); a commitment row puts the receipt in the asking thread. The
  builder's fences are unchanged: a protected file still needs the owner's tap on its card, in chat.
- **A brain-filed build request gets the same footer**: `actions.apply` appends the owner's ask and an
  acceptance line to any `## Build request` note written in a turn, so the builder has the acceptance test and
  the replay knows where to go.
- **A code change is never an operator `task`** (2026-10-28, "Results??"): a draftless `task` naming a build
  request, a hook or a `.py` file is refused by `intents.validate` (the operator drafts messages, it never
  builds, so the ask got no answer), and the brain files the `## Build request` note in the same turn.
- **The build lands -> the ask comes back by itself.** `builder.merge` calls `capability.on_built(key)`; after
  `REPLAY_AFTER_S` (the bridge restarts on new agent code first) the bridge's tick dispatches the ORIGINAL ask
  as a turn in the place it was asked, marked `_capability_replay`, with a REPLAY note in the context ("do it
  now, nothing about the wait, never ask again what they meant"). The owner never asks twice. A replay that
  takes a route closes the gap `done`.
- **A replay that still ends in "can't" builds again** (the second request names the first build and the new
  answer); after `MAX_ATTEMPTS` (2) the owner gets ONE urgent **"Build stuck"** card in that thread: *Try it
  another way* (a fresh build carrying everything tried and the owner's words from the thread) / *Drop it*.
  `cards.sweep -> _act_on_capability -> capability.owner_answer`.
- **Preferences are kept the same turn, by code.** A standing preference in an owner's line ("Never use
  semicolons in texts", "From now on ...", "I only want ...") that the turn did not `learn` goes through
  `core.rule_add` (`capability.correction`): ledgered, undoable, folded into an existing rule when it is one,
  refused for the protected core; one-off lines ("don't text him yet") and asks for a message are left alone.
- **The builder learns from its own failures.** A second attempt carries THE PREVIOUS ATTEMPT FAILED (`last_failure`)
  in its prompt instead of starting blind; a session limit or a timeout no longer burns an attempt
  (`_TRANSIENT`); a request out of attempts gets a **"Build stuck"** card (another way / drop) instead of dying
  after a commitments line; the prompt names the organization from `profile.json`, not a literal.
- **Builds no longer wait a day on a stale local edit.** `merge()` used to leave a finished build "waiting" whenever
  the live tree had uncommitted edits to a file it touched (two builds sat on `chat_push.py` all of 10-25). When
  every overlapping file is outside the protected core and untouched for `STALE_EDIT_MIN` (2 h), those edits become
  one commit of their own ("wip (builder): ...") and the merge is retried; a fresh edit or a protected file still
  waits.
- **Prompt** (generic template and live prompt together): rung 5 builds THAT piece and says the ask comes back by
  itself; a compound ask ("send the scheduling link, make a questionnaire from our meeting, loop in their
  planner") is several steps: do the ones you can, build the one you cannot, name which is which, never drop the
  whole ask over one piece; REPLAY lines explained; "a can't with nothing filed behind it is caught by code and
  filed anyway".
- **A marketplace lead is answered in the marketplace (2026-10-27, gap g-33a04e).** "Can we reply in any way?" about
  a Zola lead got "I have no way to reach them". Both prompts now say: someone reachable only through a site's own
  inbox is a `do` with the site, who and the full reply; the doer writes it there and stops at Send for the tap.
- **A forwarded marketplace lead goes straight to the doer (2026-10-27).** Paige and Casey's zola.com/z text came back
  "need date/venue". A Zola, The Knot or WeddingWire link from an owner is now a `do` that turn, by code when the turn
  sent none (`claims.marketplace_lead`): the doer reads the page, checks the date in the CRM and reports in that thread;
  a paid Connect is its checkpoint card there. `test_capability.py` section 18.

- **The brain names its own blockers (`blocked` deed, same day).** The regexes above were the first detector; the
  signal is now the brain's own: `blocked(what, why, kind)` with kind `capability` (code files the build this minute,
  `capability.from_blocked`, and the ask is replayed when it lands) or `signin` / `fact` / `tap` / `connection` (code
  records the portal and the reply says Tried / Blocked because / Need from you, once). Rung 6 of the prompt calls it;
  the reply backstop stays quiet when a turn used it. `gmcp.WRITE_TOOLS`, `intents.validate`, `actions.apply`,
  `brain.ALLOWED` (the gate baseline tracks the deny floor, so it did not move).
- **The builder demands a test that drives the ask** (`builder.acceptance_ok`): a build for a request that carries an
  **Ask** line is rejected unless a new or changed file under `assistant/tests/` shares the ask's content words (the
  fake-brain pattern of `test_capability.py` section 9); the rejection names what to add and rides into the next
  attempt as `last_failure`. "Compiles and passes" is no longer enough; "does the thing" is the bar the replay then checks.
- **A picture or a link is a `do` (gap g-53f9ec, same day).** "Can you send me a picture or a link?" (of a car title) got
  "I can't pull up a picture from here". The prompt's "cannot browse the web" line now sends any picture, link, example or
  page to find to the doer, whose `DOER_ANSWER` carries the page URL and the image URL. `test_capability.py` section 15.
- **Whether a bill or fee is paid is looked up (2026-10-30, gap g-c0da13).** "I probably paid the fee by now. So now
  what do I do with it" got "a quick search of your email or bank would settle it". Both prompts now say: `mail_search`
  for the receipt first, then a `do` that signs in to the account and reads the payment and the next step. Section 22.

Generic by construction: `capability.py` names no organization, person or service (the test checks); the
regexes are English, the names come from `profile.json` / `people.json`; `new_org.py` copies it with the rest of
the agent. State `data/agent/capability.json` (gaps, replays, cursor); `python assistant/agent/capability.py
status | scan --dry-run | replay --dry-run`. Tests: `tests/test_capability.py` (119 checks). The scan starts at the
END of the transcript on its first run (nothing from before 10-25 is back-filled); the 73 "can't" lines of
10-18..10-24 were the evidence for the regexes.

What stays with the owner, by design: a password or a sign-in (the Setup page), a tap on a send card, a fact only
they have, and the tap on a "Build needs your OK" card when a build must touch the protected core.

## One turn, one thread (2026-10-21)

the owner, after four asks for the Corwin card got no card and a reply in Corwin's thread was answered as
a team member's: "post the approval card instantly here in the thread, where we are talking about it... standard
practice". The turn was rebuilt around three rules code enforces (details in HANDOFF 2026-10-21):

- **Deeds are tool calls, not a SUGGEST line.** `gmcp.py` carries the deed tools (`task`, `do`, `call`,
  `card_revise`, `remember`, `learn`, `note`, `skill`, `org`, `schedule`, `followup`, `event`, ...); each
  validates its payload with the rules `actions.apply` keeps (`intents.validate`), records the accepted
  intent in this turn's file (`data/agent/ask/turns/<id>.intents.jsonl`, `AGENT_TURN`) and answers the
  model in words ("accepted: a card lands under your reply, in this thread" / "refused: owner's calendar
  is not connected"). Nothing is applied inside the model's process: the bridge reads the intents back
  (`intents.collect`) in the same shape `actions.apply` has always taken. The model knows what was
  accepted before it writes, so the honesty-check and card-check re-asks (two more model turns each) are
  gone; a trailing `SUGGEST {json}` line is still read as the fallback for one release.
- **The card lands with the reply, where it was asked.** `task` takes `draft` (channel, to, subject, text):
  `actions.draft_card` posts the send card itself (`cards.post(..., thread=, space=, asked=True)`) in the
  same `handle()` call, so reply and card arrive together in the thread, or in the owner's DM (`chat_space`;
  before this a DM ask's card was held for Monday). `asked=True` makes the card urgent wherever it was asked
  and SUPERSEDES the older open card on the same recipient or subject instead of a FRESHNESS / DUPLICATE
  refusal (the send path, drain and the tap are untouched). A draft to one of our own people (org.json
  `people`, matched by the number/email in their entry, their Wiki/People page or a contact saved under
  their full name) is linted as a vendor message, so a shooter's pay offer is not refused as a price
  promise to a lead (2026-10-24); leads and couples are checked as before. The `task` tool runs the card's
  exec check and draft lint first (`actions.draft_blocker`, read-only): a draft cards.post would refuse is
  answered "refused: no card was posted, because ..." and never "posted below" (Corwin's proposal,
  2026-10-22 12:49, refused DRAFT_LINT after Sage had said it was below). A `task` without a draft still goes to the
  operator, whose ticket now carries `space` and `asked` (`cards.py post --space --asked`).
- **No system voice under the reply.** The bulleted code lines are gone; only a failure after the turn (a
  calendar timeout, a refused card) is added, as a plain sentence. `held.py`'s lines and the brain-failure
  line were reworded. "Done." as a fallback is gone (a deed lead line, else silence).
- **Rules are never silently retired.** `core.MAX_RULES` is a ceiling of 150 that refuses with a reason; the
  old cap of 60 had retired the owner's own r4 "keep it short", r5 "not robotic", r6 "reply in thread" and r12
  "say plainly whether it was done" on 10-20/21 (restored), the duplicate `r67` is now `r78`, and five
  near-duplicate pairs were folded (r33->r44, r40->r50, r32->r43, r28->r45, r41->r48).
- **The prompt is generic + local.** `prompt.md` = `templates/prompt.generic.md` (no organization, service or
  person named; ~2.9K tokens, down from 6.5K, no contradictions); `prompt.local.md` holds this organization's
  records, lanes and habits (ORGANIZATION NOTES). `new_org.py` scaffolds both. Tests: `assistant/tests/test_intents.py`.

## Team spaces, and a bridge that never leaves a hole (2026-10-26)

the owner: "Sage is not responding. I also added it to a new Google space called the organization." Two causes:

- **Gaps after every build.** The builder writes `bridge.stop` after a merge; the loop ended and waited for the
  spine's 10-minute `--ensure`: four holes of 10-18 min on 10-26 (07:42, 09:13, 10:53, 12:11), one of them under
  "configure yourself to respond in the organization". The loop now treats the stop file like a code change: it
  restarts itself in place (`bridge.py` loop, `respawn`).
- **A new shared space was read but not answered.** The space reader already polls every space the app is in,
  but an owner's un-mentioned line was only judged "for the agent" in the home space; elsewhere it was ambient
  and mostly NO_REPLY. Now an owner's line in ANY shared space goes through the same addressing (the other
  owner, or the agent); a team member's line is for the agent when it names or @mentions it.
- **A team space is business-only for everyone.** `core.scope_of(convo, space)` is `business` for any shared
  space that is not the owners' home space. The turn then runs on the staff context (`brain.staff_context`,
  persona `templates/prompt.team.md` for an owner: team members read everything here), with `BUSINESS_ALLOWED`
  (the business reads plus every deed tool; no Read/Glob/Grep, mail, calendar, texts, who_is), and the WHERE
  block says it is a team space. An owner asking something personal there is told it stays in their DM.
  Tests: `tests/test_staff_role.py` (team-space section).
- **A card asked for in a team-space thread lands in that thread.** `chat_push.push_card` posts a card whose
  `chat_thread` belongs to another space into that thread's own space, not the home space (10-26: the Odette
  card handed to the operator after a draft-lint refusal opened a new thread in the home space). `tests/test_chat_push.py`.

## Never assume what they mean: thread, recency, last cards (2026-10-25)

the owner: "It should never have to assume what I am talking about: it should be able to tell by what thread it is
and what we were just talking about or what card it just sent me to approve." The WHERE block (`place.block`,
written by code on every message) already carried THIS THREAD SO FAR and the card of the thread; a brand-new
thread or a bare noun ("the project", 15:01 on 10-24, answered with "I'm guessing Kingsmere") had nothing else to
resolve against. Two more blocks, zero-LLM:

- **JUST BEFORE THIS**: the last hour of this conversation across every thread and the top level, oldest
  first, each line tagged with its thread's subject (the first line on record in that thread: the owner's
  ask, or the card header). The current thread's own lines stay in THIS THREAD SO FAR. `place.recent_block`.
- **YOUR LAST CARDS**: the cards posted in the last three hours with where each stands now: open and waiting
  for a tap (with its buttons), tapped at HH:MM and gone out at HH:MM, tapped and waiting (with the time it
  goes), or superseded. From the card store, the answers index and the exec tickets. `place.cards_block`.
  "Remake the card", "the clarification card", "the one you just sent me" resolve here, and a card that went
  is never called waiting (the 16:19 "was already sent" / 16:21 "it was never sent" pair).
- The prompt's resolution order (both prompt files): THIS THREAD SO FAR, the thread's card, YOUR LAST CARDS,
  JUST BEFORE THIS; the newest match wins and is named in a clause; "I'm guessing" / "I assume" are banned; a
  question back only when two things fit equally in the newest lines. Tests: `tests/test_context_resolution.py`.

## "It still isn't able to do everything I asked" (2026-10-25, the 10-24 afternoon and evening)

Read against the transcript, the tickets and the logs; every item below is a code change, not a rule.

- **"Stop confirming texts send. Leave that alone."** (20:52; 17:54 "I don't need to see text sent confirmations
  based on the replies"). `outcomes._verify_phone`: a text the phone has TAKEN is sent (`confirmed_by: handed`);
  no "Confirmed at ... (they replied)" line, no "Did this text go?" card, and a hand-off row (task / do) closes
  quietly because `commitments.py` is already its voice. The false "the clarification to Marlo was already
  sent" at 16:19 came from exactly that reply-based confirmation.
- **"I don't want to see messages like this. I just want you to fix it."** (17:40, 21:32 about the comms probe;
  it was "stopped" four times while running every minute). The probe only stamped `last_wake` when it woke the
  operator; `deadman.py` read that as the heartbeat. Now the probe stamps `last_tick` every run, the deadman reads
  the newer of the two, and when a heartbeat is stale it runs the FIX first (`FIXES`: schtasks /run for the
  probe and the spine, `bridge.py --ensure`) and tells an owner only if it is still stale afterwards.
- **"Approved item not run ... still queued after 7h"** on a two-minute ticket (16:45, 18:08): `sentinels._age_h`
  read local timestamps as UTC. Local now.
- **A reply card refused as "card #3 to them in 24h"** (Corwin said yes, 16:46) and **a reply held for Monday**
  while the owner was mid-conversation about Marlo (15:35): `cards._is_reply` (a "Reply to" / "Text reply" header,
  or "X replied / asked / texted") is exempt from the recipient limit, and a reply to someone we sent something in
  the last 24 h (`_live_conversation`, from the exec tickets) is urgent: Chat now, never the digest.
- **"Tell them to please ignore the previous email"** one minute after the tap (18:07) was refused
  ALREADY_ANSWERED while the email still sat in the 10-minute drain queue. `card_revise` on an answered card
  whose ticket is still queued now recalls the ticket (`cards.undo`) and posts the revision. The follow-up to
  Teo AND Wren (she asked for every email) is on a card in that thread.
- **"You should have given me reminders about it since I gave you the information"** (Tony's party, 16:56).
  `agent/event_reminders.py` (bridge minute tick, config `reminders`): one line to the owner 45 min before every
  timed event on a connected owner calendar, place included; client meetings stay with `meeting_reminder.py`;
  all-day events are the brief's. An event Sage adds itself also gets a once-reminder (`actions._event_reminder`).
- **"We already sent him an example handoff; you should be aware of that"** (16:55): `brain.sent_block()`, a
  SENT RECENTLY block in every owner turn (texts, emails, CRM sends of the last 30 h, from the exec
  tickets), so one thread knows what another sent.
- **Noise cards from one-off failures** ("CRM connector in the doer's browser down", "Mail: owner3@example.invalid
  down: TimeoutError"): `conn_check` keeps a connection live with a strike on the first failed check after a
  live one; two in a row is down.
- **"CRM connector down (code fault ... no snapshot in the transcript)"** (09:04): every headless run
  loaded the machine's user-scope MCP servers, and the `rea` server's 30 s connect timeout sat in each one.
  The connector's snapshot script, exec script and the brain's turns now pass `--strict-mcp-config` with only the servers they
  use (the brain adds the CRM connector's MCP server to its own per-turn config via `crm.MCP_SERVERS`). A forced
  snapshot ran clean right after (78 projects).
- Also closed: the "the organization email for a team member" decision card (the owner made the mailbox himself).
  Tests: `tests/test_day2.py`; `test_outcomes.py` and `test_conn_check.py` updated to the new rules.

## The staff role: a team member, wedding admin, zero personal access (2026-10-24)

the owner: "I added a team member. Allow him permission to do wedding business tasks limited to that of an admin and not an
owner. You should have zero access to my personal life information." `people.json` gained a role and a user:

- **Role `staff`** = `can: [ask, task, note]`, `lanes: [wedding]`, `paths: [Wiki/Couples/, Wiki/Vendors/,
  Wiki/Venues/, Wiki/Packages/, Wiki/Post-Production/, Wiki/Operations/, Wiki/Integrations/]`. a team member is
  `users.a team member` (owner1@example.invalid, owner2@example.invalid). `core.is_owner / owners / person_lanes / person_paths`
  read it; `owners()` is what every self-started message, reminder, card and DM discovery uses now
  (`routines.py`, `chat_page.py`, `chat_push.discover`, `gmcp._owners`), so a staff member is answered when
  they write and gets nothing else.
- **A staff turn's context is a different document** (`brain.staff_context`): `templates/prompt.staff.md`
  (the walls, in words), the organization block, memory facts of kind `business` only, the lane's skills,
  the wiki map cut to the role's folders. No rules, routines, jobs, PEOPLE WHO TEXT, connections, library,
  builder state, no `Memory/` anywhere.
- **A staff turn's tools are a different list** (`brain.STAFF_ALLOWED / STAFF_DENIED`, no `--add-dir`):
  the business connector read-only, `vault_search` and the two deeds `task` and `note`. No Read / Glob /
  Grep over the vault, no mail, calendar, texts or `who_is`. `gmcp.call` enforces the same gate server-side
  from the turn's person (`_turn_person`), and scopes `vault_search` results to the role's paths
  (`_scope_search`: Memory, Daily_Logs and Wiki/People never come back).
- **Deeds**: `intents.validate` refuses everything but `task` and `note` for a non-owner, forces the lane
  to the role's lane, refuses a `## Build request` note. `actions.apply` keeps only those two keys, blanks
  the thread and space so the card goes to the OWNERS' space (never the staff DM; a team member cannot tap Send on
  what he asked for), and prefixes the ask with "Asked by a team member (team) in chat". The receipt still comes back
  to a team member's chat ("handed on for the owners' tap").
- a team member reaches Sage by DM in Google Chat once his address is on the Chat app's visibility list
  (Cloud Console, project `sage-chat` > Google Chat API > Configuration > Visibility; GOOGLE_SETUP Part 2
  step 7 set it to the owners' addresses). That click is the owner's. Tests: `tests/test_staff_role.py`.

## "Sage needs to be able to do everything I asked it today" (2026-10-24)

the owner's asks from the 10-23 evening and 10-24, read against the transcript, the tickets and the CRM, and
what each one needed:

- **"Send it at 8am" (the Marlo text) -> "I can't time a send".** The chain already took `exec.send_at`
  (the 12:56 commit); the missing rung was an owner's WORDS answering a card. New deed **`approve`**
  (`gmcp.py`, `intents.py`, `actions.py`): in the thread of a card an owner asked for (`asked` /
  `freshness_ack`), "send it" / "yes, send" / "send it at 8am" answers that card on their words,
  `at` (HH:MM or YYYY-MM-DD HH:MM, 07:00-21:00) becomes the ticket's `not_before` (`cards.answer(...,
  send_at=)`); a card the system posted by itself still needs the tap. Both prompts: the "you can never tap
  for them" line is gone. **Skip in words (2026-10-28, gap g-bdccf4):** "he probably signed it already" in
  the Brian title-text thread got "tap Skip, I can't skip it for you". `approve` with `skip: true` closes the
  thread's card on its own non-approve button (`intents.skip_label`), nothing sent; any open card, since a
  skip sends nothing. `bridge.card_context` and both prompts say so. **Fix it and send (2026-10-28, gap g-b436c2):**
  after the approved Teo & Wren follow-up failed, "Fix it and send" got "you'll get the card in a few minutes" and the
  operator reopened it in another thread. `approve` now also takes a card the owner already approved whose send failed
  (`intents.failed_send`: consumed, newest ticket an exception, nothing queued or done; found by itself when the thread has
  no card): the same card comes back (`cards.reopen`) and is approved on their words that turn. An UNVERIFIED failure only
  qualifies for the CRM connector's message kind, whose executor reads the CRM thread before sending.
- **Marlo got two texts (12:19 and 13:10).** The first card was approved on the owner's words in a Claude
  session; at 13:09 Sage drafted a fresh card off his 1 PM reminder without looking and he tapped it.
  `intents.recent_text_to()` + `_check_draft`: a text draft to a number that got one in the last 24 h is
  REFUSED with the earlier text quoted, unless the ask says resend / again / follow-up. The prompt says to
  tell the owner the earlier one went. 2026-10-29: a reply to a text they sent after ours passes by itself
  (`intents.wrote_since`), and a wanted second text sets `draft.resend`; a marker word in the body no longer counts.
- **CRM replies never sent (Delia x2 on 10-24, Emery x3 on 10-18).** The connector pauses every
  client message for an approval card only a live Claude window can show, so headless `createCommunication`
  is always "the member did not approve this change". The CRM connector's exec script now falls through to
  the CRM browser-send script: the approved words go through the CRM's own email composer
  in the doer's signed-in browser (CDP), the feed is read before (no double send) and after (evidence
  `CRM_DONE=<stamp> STATUS=browser`). Proven live on the Harper Oakhurst workspace (its only client is the owner's
  own alias); the connector listed the message afterwards. Delia herself was answered by a team member in the CRM
  at 12:15 (his message is the newest in her thread).
  The connector's newer wording ("Approval not available for this run ... requires createCommunication approval", Teo & Wren 10-28) is caught in the CRM connector module too, so it takes the same browser route instead of an exception card.
- **"Add a team member to every new CRM project" (11:00; the builder asked for an OK and the tap was lost).**
  Built by hand: the CRM connector's add-team-member kind (addWorkspaceMember by user id, idempotent, no contact, nothing
  emailed by us; a team member is `ce9827d493d83824f4ef5036`), `assistant/scripts/team_on_projects.py` on the spine
  every 30 min: every open project (lead / lead_sent / client with a future date or activity in 60 days)
  gets a ticket under the standing rule (config `crm.team_on_projects`), 4 per run, failures
  retried once a day up to 3 times; Teo's project went first. `tests/test_team_on_projects.py`.
- **"Include a real touch point scheduling link"** -> Sage could not find one. It is the Touch Point Call
  session in the CRM scheduler: `<the CRM scheduler's public booking link>`,
  now `config.texts.checkin_scheduler_link` (so the T-14 text goes by itself again) and in memory; a card
  with that link as a follow-up text to Teo was posted in his thread (the 11:30 text had gone without it).
- **"Send the questionnaire"** -> the CRM's own automation on Teo's project is holding "Send file via
  email: Questionnaire (default)" for an Approve (the CRM's automation queue; the old-generation
  tasks API does not list it). The CRM connector's automation-approve kind runs the approve step (one click,
  the row must disappear), card-gated; the card is in Teo's thread.
- **"That draft never came back, so nothing went out"** on an ask Sage had answered in the thread at 22:55
  (Wren already on, the rest blocked, "Need from you"). `commitments._answered_in_chat`: an agent reply in
  that thread within 20 min carrying Tried / Blocked because / Need from you / already on is "with the
  owner", never a slipped draft. `tests/test_commitments_chat.py`.
- **A blocked card is said in its thread at once** (Corwin's revised proposal, 10-21: the operator's card was
  refused by the 24-hour recipient limit, the ticket was staged with that note, and it read as "a card is up").
  `commitments._REFUSED`: a task ticket staged/done with a card refusal and no card id is "failed", and the
  thread gets one plain line naming what blocked it, without waiting for the deadline. Owner-asked cards
  themselves already skip that limit (`cards.post(asked=True)`).
- **a team member in Google Workspace** (asked in the session): no token holds an Admin SDK scope and the doer's
  browser is signed out of admin.google.com, so Sage cannot create the user today; the three-minute steps
  and the seat price went to the owner in chat (the doer path for next time is
  `skills/giving-a-team-member-a-example-films-email-address.md`, after a Google sign-in in the doer browser).
- Also verified: the YouTube Scout's Windows task is Disabled (the owner: "just turn it off"); Mission Control
  answers on :9019 (the 07:09 "down" card was a blip).

## "Make it so Sage can do anything I asked it to do" (2026-10-23)

the owner, 2026-10-23. A read of every "can't / couldn't / not from here" line Sage wrote since 10-09 (82 of
them) came down to six gaps, each closed in code:

- **Our own scheduled jobs are Sage's to stop, start and run.** `switches.json` now registers all eleven
  Windows tasks this repository owns (the scouts, the dreaming, the operator, the day spine, the comms
  probe, the drive ingest, ShareGrid); a job names a config field and/or its task. `switch {job, on}`
  enables/disables the task (and flips the config field when there is one) and `switch {job, run: true}`
  starts it this minute, through `Enable-/Disable-/Start-ScheduledTask` run by the **bridge** in the
  owner's session (`switches._ps`, one batched state read cached 45 s). The registry is the allow-list;
  nothing creates, deletes or edits a task. The doer's computer policy still bans schtasks, unchanged.
  The scout that could not be switched off on 10-23 is the first customer.
- **The builder stops failing on size and flaky suites, and Sage can see it.** `MAX_DIFF_LINES` 600 →
  2000 (four owner-asked builds of 1400-1800 lines had died on it); a suite that fails after the change
  is rerun once before the build is blamed (`FLAKY_RERUNS`; test_phone_bridge failed on a second
  boundary under load and three builds were blamed); the "Build needs your OK" card is **urgent** (it
  reached chat only in Monday's digest); `builder.render()` puts a BUILDS block in the brain's context
  (runs used today of the cap, what is building, each recent request's state in plain words), so "I
  can't see how many runs it has used" is gone. `builds_requeue.py --broken-tests --too-large` puts
  those two kinds of failure back by hand.
- **A doer promise is a doer job.** "That goes to the doer, I'll tell you once it's done" with no `do`
  left the price-sheet change unqueued on 10-19 until the owner asked again. `claims.CLAIMS["do"]` now
  catches those shapes and the bridge queues the job from the owner's words + the brain's sentence
  (the same net that forces a `task` for a message ask), so the doer reports back as usual.
- **A revision asked for in a thread always posts.** `actions.revise_card` passes `asked=True` (and the
  DM space): urgent where asked, superseding the old card instead of tripping the 24-hour recipient
  limit that refused Corwin's revised proposal card twice while Sage said it was below.
- **Prompt:** `switch` covers run-now; "if your reply says a job goes to the doer, the `do` call is in
  the same turn"; a change inside a service with a connector (a stage move, a participant, a note) is a
  `task` without draft, nothing "stays manual"; read BUILDS before saying you cannot see the builder.
- Tests: `tests/test_switches.py` (task mode through a fake PowerShell), `tests/test_builder_verify.py`
  (rerun, cap, render, urgent card, requeue flags), `tests/test_intents.py` sections 10-11.

What stays a line in chat, by design: a sign-in with a password, a tap on a card, a fact only an owner has.

## "Was Sage able to do everything I requested today?" (2026-10-23, evening)

A read of the day's transcript against the logs. Of the owner's asks, five were done as asked (the a team member text, the
Corwin breakdown email, the ad cap to $400, closing a team member's camera-2 offer, the meeting summary on the second
try) and the rest hit six gaps, each closed in code:

- **A turn lost to a dead bridge is replayed.** 14:24 "Look at the meeting notes" went to a worker thread; the
  bridge process died with no log line (stderr was DEVNULL) and the spine restarted it at 15:06; the question
  was gone until "Try again". Every turn handed to a worker is now written to `data/agent/inflight.json` first
  and removed when answered (`_inflight_add/_done` in `bridge.py`); a fresh bridge replays what the old one
  left (`replay_inflight`, under 3 h old, oldest first) and the reply opens with REPLAY_LINE. `step()` is
  guarded so one bad poll cannot take the process down, and the detached bridge's stderr goes to
  `~/.claude/agent-bridge.err.log`. The terminal channel and slash commands never replay.
- **The owner's build request is built first.** 18:20 "create a skill that creates deliverables for Corwin"
  was filed as a build request and Sage said "being built now"; the run took the two OLDEST open requests
  (`collect()[:2]`) and Corwin's waited for a day that had no runs left. `builder.run(first=key)` /
  `kick(first=...)` / `--first`: the request an owner just filed heads the run `actions.apply` kicks.
- **A reply to someone who just wrote in is never "the 3rd card to them in 24 h".** Corwin said yes at 16:10;
  the card answering him was refused FRESHNESS_REPEAT_RECIPIENT and Sage asked in chat. `cards._post` fills
  `last_inbound_ts` from the comms board (`data/comms_state.json`, zero-LLM) when the author left it blank;
  the inbound side only, so it can relax the repeat guard and never refuse a card.
- **The phone never acks, so stop chasing every text.** 123 outbox fetches since Oct 6, zero
  `/api/phone/ack` calls: the owner's "example-org Send Texts" Shortcut has no ack step, and the sentinel chased every
  text 30 min after it went (a team member 10:15; Teo's Oct 18 text again on Oct 23). `phone_bridge.acks_ever(account)`
  decides the window: 30 min on a phone that acks, 4 h on one that never has (`HANDED_STALE_NOACK_MIN`),
  and nothing older than 72 h (`HANDED_GIVEUP_H`). The finding carries the date, says the text almost
  certainly went, names the Shortcut step to add (the owner_IPHONE_SETUP.md step 5), and is `once: True`.
- **One chase digest a day.** The operator relisted the same stuck items in chat at 12:10, 12:15, 16:17 and
  17:45. `sentinels.run` folds further card/staged/phone findings into the day once a chase digest exists
  (latched, no ticket); a sensor, connection, exec or spine finding is news and still gets its own digest.
  `once` findings are never re-pinged.
- **The "flaky" phone test was a re-exec.** `phone_nudge.py` called `ensure_venv()` at import, which under
  system python re-ran the whole process (SystemExit inside pytest); the builder blamed three builds on it.
  It re-execs only when run as a script.
- Also: the skill tool's refusal for a protected-core how-to now says to file a build request (Sage told the owner
  "the skill wouldn't save" for a program he wanted MADE; both prompts say a tool to be made is a build
  request, never a skill); `skills/giving-a-team-member-a-example-films-email-address.md` is the doer path
  for a team member's mailbox (Admin console, auto-generated password, sign-in instructions emailed by Google, Add is
  the checkpoint; Sage never sees a password); the YouTube Scout's Windows task is disabled as well as its
  config switch (the 09:23 switch predated task mode). Tests: `tests/test_today_gaps.py`.

What stays with the owner: adding the ack step to the Shortcut on his iPhone (the finding tells him how, once),
and the taps on the open decision cards (a team member's mailbox kind; the $1,000 deposit, which his own text to a team member
already settled).

## Decision cards, triage, build now (2026-10-23)

the owner, 2026-10-22: "Sage should build things right away when it solves an immediate problem. Sage should
triage better like example does. Instead of ending with 'would you like this or that', Sage should just make
approval cards for the next decision with a recommendation when applicable." Three small pieces
(plan: `PLAN-build-now-triage-decide.md`):

- **`decide` is a deed tool** (`gmcp.py`, `intents._validate_decide`, `actions.decide_card`): a decision that
  is the owner's becomes ONE card with 2-3 short buttons (at most 5 words each), the recommended one FIRST
  and tagged "(Recommended)" (what `cards.py`'s NO_RECOMMENDATION rule wants), one sentence why on the card,
  urgent, in the place it was asked (`asked=True`, thread or DM). The card has **no exec**: a tap only
  records the answer, the bridge posts "[tapped ...]" back into Sage's conversation and the next turn acts
  on it with the usual tools, so no decision button sends anything by itself (send gate unchanged). A
  message dressed as a decision ("Text to Maya ...") is refused and pointed at `task` + `draft`.
- **A chat-filed build request is kicked at once.** `actions.apply` calls `builder.kick(force=True)` right
  after the `## Build request` note saves; `force` skips only the hourly gap and the daily cap (the
  protected-core fence, the needs-owner card and MAX_ATTEMPTS hold). Dream-filed requests keep the
  throttled path. The tool answer now says "the build starts right after this reply"; the prompt says to
  say it is being built now.
- **Triage like example, in the prompt** (`templates/prompt.generic.md` and `prompt.md`): sort each line into a
  LOOKUP / WORK / DECISION before answering; a manager, not a relay; the turn ends with the next action,
  never "would you like A or B"; the one chat question allowed is for ONE fact that cannot be found;
  blocked = "Tried / Blocked because / Need from you". Rung 4 of the ladder is now `task`+`draft` for a
  message and `decide` for a choice; rung 6 lost "a decision only they can make". Tests: sections 8-9 of
  `assistant/tests/test_intents.py`.

## One receipt per hand-off (2026-10-21, Phase 2)

`receipts.py`: every hand-off (a task, a doer job, a build request, a card) owns exactly ONE Chat message
in the place it was asked (the thread, or the owner's DM), keyed on its reference (ticket id, card id,
note path). The first outcome posts it, every later outcome EDITS it (`gchat.patch`), an unchanged
outcome is a no-op, and the transcript carries each state (an edit marked `edited`). Routed through it:
`commitments.check` (the bridge passes `receipts.poster()`; `_deliver` hands the row's ref along),
`doer.deliver` (a DM ask's report now lands in the DM; before this it went top-level in the home space).
The slipped lines are plain ("Still not built: ... say so once more and I'll file it again"), "I'm not
going to keep repeating it" is gone, and `commitments._builder_said` no longer silences a build's
confirmation (the builder's own note is transcript + Mission Control only, so until now a built request
was never confirmed in the conversation that asked for it). `cards.reopen` marks the old Chat message
closed ("not confirmed done; the card is posted again below") before the card comes back, and
`chat_push.goes_out_line` says the real timing on an answered card (texts: about 36 s to the phone;
everything else: the next pass within 10 minutes, outbound kinds held overnight) instead of "about 35 s"
for everything. The chase's folded-items line goes to Mission Control as a note, not the family chat.
Tests: `assistant/tests/test_receipts.py`.

## Speed and resilience (2026-10-21, Phase 3)

- **Context deltas** (`brain.context_delta`): a resumed session whose rules, memory, schedules, skills,
  organization or connections changed gets a `WHAT CHANGED` block of added and removed lines, not the whole
  ~20K-token context again (a change past 40% of it is still a full refresh). The last context each
  conversation saw is kept in `data/agent/ctx/`.
- **Unblocked loop**: `loop()` runs each conversation's turns on its own worker thread (one in flight per
  conversation, the rest queued) and the minute ticks + the commitments check on a tick thread
  (`_tick_loop`), so a long turn, a voice transcription or a follow-up fire never holds up another
  conversation. `brain._TURN` and `actions.LAST_APPLIED` / `LAST_FOLLOWUPS` are thread-local;
  `sessions.json` writes are locked. Tests call `step()` with `ASYNC` off and keep the old synchronous path.
- **Placeholder watchdog** (`placeholder_watch`): a "typing" placeholder older than 90 s is edited once
  ("Still on it; this one needs a look at the records"); a crashed turn edits its placeholder to one honest
  line instead of leaving "is typing" up for good; a voice note shows "typing" before the transcription.
- **`texts_search`**: a read tool over the phone thread log (both iPhones) by words, number or saved name,
  so "my dad texted" / "Marisa texted, let's reply" resolve without asking. Every line is labelled by
  `whois.py` ("Dad (the owner's dad, +1 555-063-5563)", "probably Nicole (+1 ...)").
- **Who is texting (2026-10-23).** the owner: "Sage has a difficult time sorting through and deducing who people
  are from inbound text messages." Three pieces, all on `assistant/scripts/whois.py` (its docstring is the
  source list; `python assistant/scripts/whois.py who "my dad"`):
  - **PEOPLE WHO TEXT** rides in the brain's context (`brain.texters_block`): every human who texted the
    owners' phones in the last 30 days, best name with how sure ("probably"), relation, the last line, group
    texts marked with who started them, automated senders folded into one line. Sage no longer has to search
    to know that +1 555-063-5563 is Dad.
  - **`who_is`** (read tool): one number or email, or a description ("the mechanic", "the one asking about
    the Nikon"), answered with the evidence: contacts, memory, the wiki People / Couples / Vendors notes, the
    leads, the tracker, the CRM, and the texts themselves (how the owner opened a text to them, "Hey
    Marisa"; how they signed, "Mira Tsai: Hi the owner"; who the group addresses, "Praying for you, Nicole"; a
    number one digit from a known one; promo / code / campaign markers). Candidates are merged noisy-or
    style, so two weak clues that agree become a sure name.
  - **`identify`** (deed): "that's my dad" / "that was Diane, Frank's wife" saves the alias
    (`data/people_aliases.json`), names the thread in the log (so cards and the comms board show it), adds
    one memory fact and ledgers an undo (`alias_forget`). Owners only.
  The phone bridge uses the same answer for every inbound text (ticket title "Text from Dad: …" /
  "Text from probably Nicole (+1 …): …", `payload.who` with name, relation, kind, confidence, why, group),
  the spine job `whois-harvest` writes sure names into the thread log hourly, and the operator prompt points
  at `whois.py who`. Rules of voice in `prompt.md`: call people what the owner calls them, say a "probably"
  in one clause and act, never ask who someone is when the texts say. Tests: `assistant/tests/test_whois.py`.
  Still missing: the iPhone address books were never imported (`/contacts` on Mission Control); until they
  are, "saved" names come from the texts, memory and the records.

## Self-improvement, chat-contained setup, the template (2026-10-21, Phase 4)

the owner: "add self improvement and chat contained usability and connections so Sage can become a business
assistant to other organizations some day".
- **Rules merge instead of a cap**: `dream.merge_rules` runs at the end of the nightly pass and folds
  near-duplicate active rules (`pending.similar_text`, the rewordings `core.similar` lets through at add
  time): a dream-written rule folds into the earlier one, an owner's rule is never retired here, each fold
  is a ledgered, undoable retirement.
- **Nothing of this organization in the code**: owner words for the safety regexes (`core._OWNER_WORDS`),
  the fallback DM (`core.first_owner()`), the tool enums and mailbox lists (`gmcp.OWNERS`, `_mailboxes`) all
  come from `people.json` / `google.json`; the deed tools speak of "a service they use" and `channel:
  service` (the CRM connector's channel; connector-provided aliases accepted via `CHANNEL_ALIASES`), the generic prompt names no organization.
  `assistant/tests/test_template.py` enforces it (no `"dm:the owner"` literals, no owner names in a regex, no
  organization or service in a tool description or the generic prompt).
- **From the phone**: `/health` (zero-LLM: heartbeat, turns today and their timing, open and held cards,
  held questions, open hand-offs, connections down with the Setup link, turns in flight) and `/setup` with
  the Setup page link. A remote sign-in portal (the PC's browser view from the phone) stays a build request.

## How a message flows

```
phone chat page / terminal ->  bridge.py  ->  brain.py (one read-only model turn, session resumed)
                                                 | reply text; deeds recorded in-turn by the tools (intents.py)
                               actions.py  <-----+   (a trailing SUGGEST {json} line still read as the fallback)
        learn/unlearn -> state/rules.json        note     -> Wiki/ (draft, linted, committed)
        remember/forget -> state/memory.json     schedule -> state/routines.json
        event -> the owner's Google calendar     task     -> the operator's ticket queue (cards)
                                                 do       -> the doer (assistant/doer): a browser agent that
                                                             acts on sites, stops at a card before anything
                                                             irreversible, and answers back in this chat
                               |
              confirmation lines written by code, appended under the reply
```

The model never writes and never sends. "Saved" is only ever said by the code that saved it.

## Files

| File | Role |
|---|---|
| `profile.json`, `people.json`, `prompt.md`, `google.json` | everything particular to this agent and household |
| `core.py` | rules, memory, the protected-core filter, the change ledger and undo, the transcript |
| `brain.py` | the read-only model turn; one session per conversation per day; context re-sent only when rules, memory or schedules change |
| `actions.py` | validates and applies the SUGGEST line; returns the confirmation lines |
| `wiki_writer.py` | the only path into `Wiki/`: schema front matter, `status: draft`, lint, scoped commit; appends to an existing note, never rewrites one |
| `followups.py` | check-backs on something a person is meant to do or answer, and promise-keeping (the code example and example run, ported) |
| `routines.py` | schedules the agent makes (`say` = fixed text, `prompt` = a fresh read-only look-up); fired by the bridge every minute |
| `meeting_reminder.py` | ~10 min before a lead vision call or CRM meeting (Google calendars + the CRM snapshot), one line to the owner hosting it: start time, Meet link, who else is expected; once per meeting, fired by the bridge every minute |
| `google_api.py`, `gmcp.py` | Google calendars and mail; `gmcp.py` is a private read-only MCP server attached to the model turn |
| `gchat.py` | the Google Chat channel (Pub/Sub pull, posts as the app) |
| `space_reader.py` | reads shared spaces and fetches attachments with the owner's example sign-in; local voice-note transcription |
| `bridge.py`, `chat.py` | the loop over all channels; `chat.py` is the terminal channel |
| `chat_page.py` | the phone chat, served by Mission Control at `/chat` (https://<this-pc>.<tailnet>.ts.net:9443/chat); it only queues a line for the bridge |
| `account.py` | which Claude subscription the model turns run on (`profile.json` → `claude_account`, read from the machine's DPAPI account store) |
| `dream.py` | nightly learning over the day's conversations + the daily digest; `pass` = a daytime learning pass every 3 h 09:00-22:00 (spine job `agent-dream-day`, skips under 6 new lines, posts only what it changed) |
| `state/` | tracked in git: `rules.json`, `memory.json`, `routines.json` |
| `assistant/data/agent/` | git-ignored: transcript, change ledger, sessions, inbox/outbox |

## Reply logic (ported from example's bridge)

- **One burst, one answer.** Several quick messages from one person are joined into one ask. Since
  2026-10-21 this spans polls: a Google Chat line waits in `data/agent/burst_pending.json` until its
  sender has been quiet for `profile.json -> burst.settle_s` (20 s), so three lines typed 5-30 s apart
  are ONE brain turn and ONE reply (`bridge.step` / `_burst_*`). A slow typist's lines wait
  `pace_factor` (1.5) x their largest gap, so a line every 40 s still gets one answer; no group waits
  past `max_wait_s` (120 s). The typing placeholder goes up the moment the first direct line is
  buffered and the reply edits it. The buffer is on disk, so the bridge's self-restart on a code
  change loses nothing. Slash commands and the local channel (terminal, tests) never wait. Lines
  all created before Sage's latest reply in that conversation carry a "they have not seen your last
  reply yet" note into the brain's context. Tests: `assistant/tests/test_burst.py`.
- **Typing placeholder.** On Google Chat `_Sage is typing…_` is posted at once and edited in place into the answer.
- **NO_REPLY is silence.** Nothing is posted and the placeholder is taken down.
- **Chat markup.** Replies are converted to Google Chat's own bold, bullets and links.
- **Follow-ups.** "Ask owner whether Saturday works" or "I'll send it tonight" becomes a tracked check-back
  (`followups.py`). When it comes due the brain checks whether it is settled; only if not does that person get
  one line. A promise to check back that nothing is tracking is removed from the reply.
- **Addressed vs ambient.** `space_reader.py` reads the shared spaces with the owner's example sign-in (read-only
  scopes), the way example reads her space, so people talking to each other are seen too. @mentions, the name "Sage"
  or a question aimed at it are direct; the rest is AMBIENT: no typing placeholder, at most 4 model turns an hour,
  and the brain answers NO_REPLY unless it holds a fact they need.
- **Voice notes and photos.** Attachments are fetched with the reader's sign-in; audio and video are transcribed on
  this machine (`scripts/transcribe.py`, faster-whisper) and handed to the brain as the person's words; photos as a
  path the brain opens with Read.

## One space, and a human voice (2026-10-07)

the owner: "all Sage communication in one space with owner and I", "avoid status reports that look
robotic", after Sage answered one message three times. Three things follow from that:

- **The home space.** `profile.json` → `home_space` (today `spaces/AAQA6LAmbsA`, the "Sage" space
  with both owners). Everything the agent says on its own goes there once: cards and notes
  (`chat_push.targets`), the morning digest, routines to "both", the doer's reports and the
  builder's notes all route through `bridge.post`, which posts to the home space and drops a
  second copy of the same text within two minutes. A DM is still answered in the DM. In the home
  space an owner's plain line is for the agent; a line that starts with the other owner's name
  ("owner, can you...") is theirs and stays ambient. Empty `home_space` = per-owner DMs again.
- **One answer per message.** Chat and the Apps Script relay re-deliver an event they think went
  unanswered; `gchat.queue_inbound` keeps one copy per message name, and `bridge._dedup` drops
  anything already in `seen_messages.json` or repeated in a batch (by name, or by place + sender +
  text when there is no name). Before this, 2026-10-07, "Fix the CRM connector" arrived three
  times in one turn and Sage answered each.
- **No code-made announcements.** `actions.apply` adds a line under the reply only when something
  could not be done; successes are Sage's to phrase ("Got it, I'll remember that"). Undo ids live in
  the ledger (`/changes`, `/undo cN`) and on Mission Control. The morning digest in chat is a few
  plain sentences (`dream.digest_chat`); the full list with ids goes to Mission Control only, and
  the daytime learning passes say nothing in chat at all.

## Borrowed from the property OS (2026-10-08, crossover read of example's code)

the owner: "triage and context awareness are not on the level of the property OS; read how it does
it and learn from it." Tools only, none of the property rules:

- **The ack gate** (`ack.py`, example's `chat_agent/ack.py`): a message that is only an
  acknowledgment or a tapback never reaches the model. In Chat, "ok thanks" gets no typing
  placeholder and no turn; on the phone lane, `comms_probe.pretriage` dismisses "Thank youuuu" /
  "Liked an image" by code before any wake, so the drafter only ever sees messages that need a
  human-grade look. A bare "ok" or "yes" still goes through: it can be the answer to a question.
- **The owners' chat injected into the drafter** (`scripts/owner_chat.py`, example's
  `lane_chat.inject_block`): the operator's tick gets the last day of the owner's and owner's chat
  with Sage appended as DATA, so a text is judged knowing where they are and what they said they
  want. It never authorizes anything.
- **Thread state as the substrate, not messages** (their `coordinator.py`): `comms_state.py`
  already folds the phone threads; what changed today is the judgment rule around it: a friend's
  text is never silently dismissed (reply card or Reply / Skip card), and every dismissal is
  listed in the operator's report.
- **Replies in a card's thread edit the card** (`bridge.card_for_thread` / `card_context`,
  `actions.revise_card`, `cards.supersede`; example's lane-chat rail "a message is data, the tap is
  the approval"). An owner writing "shorter" or "don't mention the price" under a card that would
  send a text or email gets a revised card in that thread with the new text; the old card is
  retired as superseded. Nothing is answered or sent by words: "send it" gets "tap Send on the
  card". Edit & send's dialog stays as the other path.
- **Who is this line for?** (`addressed.py`, example's tier-2 classifier, second crossover read
  2026-10-08): the certain rules stay zero-LLM (@mention or "Sage" = direct; a line that starts
  with the other owner's name = theirs). The ambiguous middle in the home space goes to Haiku
  (no tools, ~5 s) with the roster, the last 12 lines and the owners' labelled corrections; on
  any failure the old rule applies (an owner's line is for the agent), so nothing is dropped.
  "That wasn't for you" / "I was asking owner" teaches it (SUGGEST `addressed`).
- **Weak direct**: group talk, or a reply in the agent's thread meant for someone else, gets no
  typing placeholder and NO_REPLY stays silent.
- **Session size guard** (`brain.session_big`): a resumed model session past 1.5 MB is rotated
  (the property server found a 1.2 MB --resume made every turn slow).
- Not ported: `fact_gate.py` (units and work orders) and `handoff.py` (two agents on a bus);
  both are property-shaped. Their lesson, "check the records before you chase", is the
  follow-up tracker's job here.

## Thread roots (2026-10-11, late)

A reply "in thread" is only as good as what the agent knows started the thread. `core.note_thread_root`
remembers the root of every message the code posts (`bridge.post`, `post_in`, `reply`), `transcript_thread`
puts that root first, `bridge.resolve_thread_root` fetches an unknown root from Chat once (and caches a miss),
closed cards still resolve, and when nothing is on record the brain is told to ask which message rather than
guess. Store: `data/thread_roots.json`. Tests: `assistant/tests/test_threadfix.py`.

## Calendar adds that time out (2026-10-14)

When Google times out adding an event, `actions.py` tries again after 2 s and 5 s, all with the same
event id, so a retry can't create a duplicate. If it still fails, the add goes into `event_retry.json`
and the bridge's minute tick (`actions.retry_events`) tries again every 5 minutes, up to 3 times. Sage
says how it went in the thread where the event was asked for (the asker's DM if there was no thread).

## Five more example ports (2026-10-16, evening; the owner: "build all five")

Read from the property vault's `scripts/turn_agent/` by path (tools only, none of the property rules):

- **Never a bare action card** (`bridge._deed_lead`, example's `_card_lead`): a NO_REPLY whose SUGGEST line
  still queued a task, a doer job or a build request now gets one plain lead line ("I'm having that
  drafted: ... It comes back to you as a card") instead of silence followed by a card out of nowhere.
- **Stay out of a human exchange in the agent's thread** (`bridge._human_exchange_in_progress`, example's
  dream c20-c24): when the last line by someone else in a thread the agent started is a human
  @mentioning (or opening with the name of) another human, the sender's next line answers that person.
  With a question or eight-plus words it is a WEAK direct (no placeholder, NO_REPLY silent); a plain
  line stays ambient. Before this, `_just_spoke` made it a strong direct and the agent could butt in.
- **Collapsible grouped cards** (`chat_push.card_body` `groups`, `weekly._groups`; example's approvals card
  by unit): a many-row card carries `groups: [{title, rows}]` and the Chat card renders one collapsible
  section per subject with the first row visible. The numbered `question` text stays (phone preview,
  Mission Control, the exec verbatim check). The weekly record check groups by the part of each
  finding's title before the colon (the couple), else the lane.
- **Three rules** (`state/rules.json` r25-r27, ledger c170-c172, `/undo` works): "finishing today" is not
  done; "deal with it now" means answer in the same reply with options and drafts, not a check-back;
  check the email thread as well as the CRM, texts and the calendar before saying there is no date.
- **Fold, don't park** (`tracking/chase.py` `folded`, `folded_line`; example's FOLD_AFTER): after
  `max_follow_ups` unanswered asks an item folds: no more cards, one quiet line a day per owner
  listing what folded (`tracking.json -> chase.fold_daily_line`), a tap on its old card still counts,
  "Remind me tomorrow" resets the count, and after `park_days` it may be asked again.

Tests: `assistant/tests/test_chat_agent_ports.py` (24) and the fold checks in `test_tracking.py`.

## Questions held while the account is at its usage limit (2026-10-16)

The Claude subscription the turns run on (`profile.json` → `claude_account`) has a rolling usage
limit; when it is hit the CLI answers `You've hit your session limit · resets 7:50pm` and every turn
fails until then. Until today the bridge said "I could not answer just now. Give me a minute and ask
again" each time and the question was lost (Sage's own build request from the 10/15 nightly review:
three asks, six useless lines, nothing saved). Now, in `held.py`:

- the reply names the reset time and says the question is kept ("I've hit my usage limit until
  7:50 pm. I've kept your question and will answer it then, no need to resend it");
- the message, its place and thread go into `data/agent/held.json`; until the reset nothing else
  spends a model turn: a new direct message is kept too with one short line, an ambient line is kept
  silently;
- the bridge's minute tick (`held_retry`) re-runs each kept message 90 s after the reset, in the
  place it was asked, with no second transcript line; a limit on the retry re-holds it with the new
  time, three times at most, then one honest line and the row closes;
- a crash or timeout is not a limit: the plain retry line as before, nothing kept.

`python assistant/agent/held.py` shows what is waiting. **Optional fallback:** `profile.json` →
`claude_account_fallback` names a second stored account (`~/.claude/accounts.dpapi.json`, e.g.
`max20x`); when set, a turn that hits the limit runs once more on that account before anything is
held. Set to `max20x` since 2026-10-26; a limit recorded under another fallback setting no longer
gates anything and its held questions run at once. The builder (a clean worktree, then one more build)
and the doer (browser and computer use; a cut-off job resumes its own session) switch the same way
(`account.fallback()` / `account.is_limit()`). Tests: `assistant/tests/test_held.py`,
`assistant/tests/test_account_fallback.py`.

## From the transcript review (2026-10-11)

the owner asked for every miss in Sage's chats to be fixed. What changed (details in HANDOFF.md 2026-10-11):
calendar `event_change` / `event_remove` for the agent's own events (ledger allow-list, listed each turn),
eight events a reply, `core.tightens()` so narrowing rules apply at once and only loosening ones queue,
`bridge.recently_answered` so a re-delivered or resent message is answered once, the prompt's "How to
sound, what never to say" section (no sending owners to the PC, no mechanics talk, pronoun -> newest card,
direction on a choice card -> the draft), plain error lines, and `email_send` threading a "Re:" by mailbox
lookup. Tests: `assistant/tests/test_sagefix.py`.

## Words and deeds, promises and skills (2026-10-09)

the owner: "make Sage way better at what she is supposed to do ... personable, always knowing exactly what
we're talking about ... never saying I can't do that from here ... never saying that something was done
and not being able to do it and then not telling us later ... constant improvement and learning like a
human would." Five pieces, all code around the same read-only brain:

- **`claims.py`, the honesty check.** Each claim shape in a reply ("I'm sending the builder a request",
  "the operator will draft that", "I'm putting it on your calendar", "I'll remember") maps to the SUGGEST
  fields that would honour it. A claim nothing backs is sent back to the model ONCE in the same session
  (`claims.retry_note`); if the second reply still has no deed behind the words, one short correction line
  goes under the reply (`claims.honest_line`). Until this, 2026-10-08: "you said you are changing the
  text ... but you didn't give me a new approval card, so nothing will actually happen".
- **`commitments.py`, promises kept by code.** Every hand-off `actions.apply` really queued (an operator
  `task`, a doer `do`, a wiki `## Build request`) becomes a ledger row with the ticket id or note path and a
  deadline. The bridge checks the queues every 5 minutes with no model: a draft that never came back, a
  doer job that blocked, a build that failed twice (before this, dropped in silence) each get one plain line
  in the conversation and thread the promise was made in, then one more, then the row closes as unkept. A
  build that lands is confirmed in that thread in one short line ("That's built now: "<ask>". <gist>. Say if
  anything's off."; the gist is the summary's first clause, never its assumptions or test talk); a build that needs an owner's
  decision gets the builder's "Build needs your OK" card (approve / another way / drop, acted on by the
  tap), never a plain line or a "Got it" that records nothing (2026-10-17). Nothing at night. `/promises` lists what is open; closed rows feed the nightly
  review as `OUTCOME` lines so the dream learns from what happened, not only from what was said.
  A doer job that blocked has already said so in its own report, so that row closes quietly (one failure
  message, not two; 2026-10-09).
  A build is confirmed once only: `data/announced_builds.json` remembers each build request's title, so a
  second row for the same build (chat + nightly review) closes without a second "That's built now" (2026-10-10).
  It also remembers the build's commit, and a build the builder's own "I built ..." note already carried is
  not announced again; the line carries the whole summary instead of cutting it off with "...." (2026-10-11).
- **`skills.py`, how-tos it teaches itself.** `assistant/agent/skills/<slug>.md` (git-tracked): when it
  applies, the steps, what to watch out for, a history. Written through SUGGEST `skill` (from a doer
  report, an owner's walkthrough, a correction) or by the nightly review; the same title appends a dated
  section and merges triggers. The context lists them; a `do` whose text matches a skill's trigger carries
  the skill's steps to the doer. A skill is procedure only: the memory refusals (permissions, secrets) apply.
  `/skills` lists them. This is how odd jobs (Lensrentals, a DMV form) get easier each time.
- **`place.py`, where am I.** Every ask carries a WHERE block written from the message's place: a DM, the
  home space top level (both read), a thread (with THIS THREAD SO FAR replayed from the transcript, which now
  stores the thread of every owner line, agent reply and card), a card's thread. The prompt says how to
  answer in each. When an owner refers back ("I said", "I told you", "like I said"), their own messages
  from the last 24 hours, from every thread and the top level, are added so Sage can find what they meant.
- **The wiki, always in reach.** `gmcp.vault_search` runs `scripts/semantic.py search` (local embeddings)
  with keyword fallback, and `brain.wiki_map()` puts every note title by folder into the context (cached on
  the index's mtime). `wiki_writer.write` now kicks a detached index + semantic rebuild, so a note Sage
  writes is searchable minutes later ("cat litter" had been finding a colour-grade note: 4-day-old store).

`prompt.md` was rewritten around these: a family voice, the WHERE rules, the "never I can't do that from
here" ladder (records, change it yourself, `do`, `task`, build request, say exactly what blocks), the
words-and-deeds contract, and learning all the time. `dream.py` gained `skills` and `build_requests`
outputs (a build request is a wiki note the builder picks up, registered as a commitment; a protected one
waits in `## Pending`). Two fixes rode along: `core.DATA` falls back to `ASSISTANT_DATA/agent` so a test
that scopes only the assistant's store can no longer write into the live transcript (2026-10-09 06:30 the
dreaming self-test's fixture cards landed in Sage's conversation; the 17 rows were quarantined to
`transcript.backup-2026-10-09-fixture-leak.jsonl`), and `cards.post` records a card after its Chat message
names are known so the transcript row carries its thread.

## Learning, in three speeds

1. **In the conversation.** "From now on...", "remember that...", "remind me every...",
   "make a note about..." apply at once and take effect on the next message.
2. **Every night (Day Spine job `agent-dream`, about 06:00).** One model turn rereads the
   day's conversations and proposes what was implied but never saved. Code applies it only
   if each change cites a real transcript line, within nightly caps (`profile.json` → `dream`).
   A rule an owner taught is never retired by the dream.
3. **Through the vault's own dreaming (06:30).** The dream appends the day's chat to
   `Memory/Daily_Logs/`, so the existing consolidation turns it into `Memory/memory.md`
   facts, wiki promotions and skill patches like any other session.

Every change gets a ledger id. The daily digest lists them; `/undo c12` reverses one.

## Building its own improvements (builder.py, 2026-10-07)

the owner: "full hands off". Sage itself stays read-only; when feedback needs a code change it writes a
wiki note with a `## Build request` heading (rule r7), or its review says "needs a code change". The
Day Spine job `agent-builder` (hourly 07-23, max 4 runs a day) starts `builder.py run` detached: a git
worktree off master, one headless Opus turn per request (Edit/Write in the worktree, Bash only for
py_compile, git diff/status and the test files, scratch data dirs, no Chat), then verify. A build ships
only if it touches nothing in `PROTECTED_PATHS` (send path, cards/approvals, money, credentials,
footage, gates, `core.py`, `brain.py`, the builder itself), deletes nothing, stays under 600 diff lines,
compiles, keeps every previously passing test passing and passes `check_gates.py`. Then it commits,
fast-forwards master (waits if your uncommitted edits overlap), restarts the bridge if agent code
changed, marks the note built, and posts one Chat note listing each change with `git revert <hash>`.
Requests that need the protected core or an owner's decision are never built on their own and never
stall (the owner 2026-10-18, "everything needs to be accessible through Sage google chat"): `ask_owner`
posts ONE "Build needs your OK" card in chat naming the files (Build it, I approve / Build it another
way / Drop it; a hard path such as the gates, `.claude/`, the builder itself or live data offers only
the last two). The tap comes back through `cards.sweep -> cards._act_on_build -> builder.owner_answer`:
approve lets the model edit exactly those files for that request (`owner_ok`), another way forbids
them (`avoid`), drop closes it; the builder is kicked at once (gap and daily cap waived). Files the
test run itself leaves in the worktree (the doer's `tools/meta.json`, now git-ignored) are never
counted as the build's. The dream files protected build requests like any other now (flagged in the
note) instead of parking them as "not filed" under `## Pending`; `collect()` also picks up the ones
parked before. Two failed attempts and a request is dropped. `python assistant/agent/builder.py status` shows the
queue; state in `assistant/data/agent/builds.json`, log `~/.claude/builder-wv.log`. To pause it set
the spine job `enabled: false`.

## The owners' approval queue in chat (pending.py, 2026-10-18)

Everything only an owner may approve waits under `Memory/skill-suggestions.md` `## Pending`. By
Oct 18 that held 90 entries: the review re-filed the same few asks every night, reworded, and
nothing put them in front of anyone. Now:
- **No duplicates.** The dream sees what is already waiting (`dream._waiting_block`) and is told not
  to raise it again; `core.log_suggestion` refuses a Pending entry that matches an open one or one
  an owner already answered (`data/agent/pending_latch.json`), by content words (`pending.similar_text`).
- **A card for each.** Spine job `agent-pending` (every 10 min, 07-22) folds repeats, closes the
  builder's dropped requests, ages out asks nobody answered in 7 days, and posts one card per entry
  in the home space, at most 3 open at once (the stack folds them): a protected **rule** (Add the
  rule / Drop it), a **retirement** of a rule an owner taught (Retire it / Keep it), a review **ask**
  (Have Sage do it: an operator ticket, outward steps still come back as cards; an ask about CRM
  Scheduler hosts, which no connector covers, goes to the doer's browser instead and reports the Preview
  slot count before and after; a check that needs eyes on a sheet or a site, like cells A1-A4 on the
  pricing sheet, goes to the doer read-only and reports what it sees / Drop it), a vault
  dreaming **patch** (Build it: filed for the builder with the owner's OK, hard paths never / Drop it).
  Code-change asks stay the builder's ("Build needs your OK" cards).
- **The tap is done by code** (`cards.sweep -> cards._act_on_pending -> pending.answer`): the entry
  leaves `## Pending`, is recorded under `## Applied automatically` or `## Dismissed`, and is latched.
`python assistant/agent/pending.py status | tick [--dry-run]`; tests `assistant/tests/test_pending.py`.

## What it can never change by itself (the protected core)

`core.py` refuses any rule or fact about: sending to anyone outside the household,
approvals, paying or moving money, price or delivery-date commitments, credentials,
deleting, footage, or its own tool list. Those are queued in
`Memory/skill-suggestions.md` `## Pending` for an owner to apply by hand. Outward work goes
to the operator as a `task` and comes back as a card to approve (`assistant/README.md`).
Work that must be done on a website or in an account goes to the doer as a `do`
([assistant/doer/README.md](../doer/README.md), 2026-10-05): it signs in from its own vault (Sage
never sees a password), does reversible things itself and stops at a checkpoint card before
anything irreversible.
The Google tokens carry no send or draft scope. `brain.py`'s tool lists are frozen in the
gate baseline (`scripts/guardrails/check_gates.py`, target `agent-brain`).

## Run it

The bridge is kept alive by the Day Spine (job `agent-bridge`). Start it from the spine or a
terminal, never from inside a Claude session: model turns authenticate with the User-env
token, which a session scrubs from its children.

```bash
python assistant/agent/bridge.py --status
```

```bash
python assistant/agent/chat.py --as the owner "what is coming up this week?"
```

```bash
python assistant/tests/test_agent.py
```

Slash commands in any channel, no model turn: `/rules` `/memory` `/schedules` `/changes` `/undo cN` `/help`.

**Switches (2026-10-23).** An owner can turn a standing system job on or off in chat ("turn the YouTube scout off"). The `switch` tool (`gmcp.py` → `intents.py` → `actions.py` → `switches.py`) flips exactly one true/false field in one config file, keeping the file's layout, and ledgers it (`/changes`). The switchable jobs are data in `switches.json` (key, name, aliases, config path, field); add a job there, never in code. Their current state rides into the brain's context under the schedules (`routines.render`), and `/schedules` shows them too. Owners only. Tests: `assistant/tests/test_switches.py`.

After editing code in this folder, stop the bridge (`bridge.py --stop`); the spine restarts
it within 10 minutes on the new code. After changing `brain.py`'s tool lists, re-freeze the
gate baseline (`python scripts/guardrails/check_gates.py --freeze`) and commit both.

**Rebuilding or cloning this:** [GOOGLE_SETUP.md](GOOGLE_SETUP.md) is the step-by-step runbook of the whole setup (both Google projects, the two organisation-policy walls, the grants, the reader, the Claude account, the spine) with every problem hit on day one and its fix.

## Learning any organization, the Setup page, and the library (2026-10-12)

the owner: "make Sage in such a way it could learn any organization's needs and have simple functionality
buttons to guide owners through connections and logins on their computers ... also a simple wiki and
resources upload option in chat." Three pieces, all around the unchanged read-only brain:

- **`org.py` + `state/org.json`: the organization profile.** Name, what we do, where, hours, tone, people,
  lanes, services (name, url, what for, account name: never a secret), standing duties, never-list. Filled
  from chat through SUGGEST `org` (upsert by slug, ledgered, `/undo cN` restores the previous profile), by
  `python assistant/agent/org.py set|service add`, or by hand. Rendered into every turn as ABOUT THIS
  ORGANIZATION. **The onboarding interview is code:** `next_questions()` knows what is still unknown
  (eight questions, in new-hire order); the bridge puts ONE into the context per direct message, at most
  three a day, never the same one twice in a day, so a new agent asks naturally and remembers. A duty with
  a `schedule` becomes a routine the same minute. Protected-core text and loosening never-rules are
  refused. the organization' profile was seeded from CLAUDE.md (onboarding complete). `/org` shows it.
- **The Setup page (`scripts/setup_actions.py`, `scripts/setup_page.py`, Mission Control `/setup`).**
  One row per `connections.json` entry plus every org service with a url, sorted needs-attention first,
  with ONE button each: `terminal` (a console on the PC running the row's own setup command, the secret
  typed hidden there), `env` (one hidden value -> User env var), `window` (a CRM sign-in window),
  `signin` (the agent's browser on screen at the site; the session persists in the doer profile),
  `open_url`, `mcp` / `text` (instructions). **Check** re-runs conn_check (or probes a service's url
  off-screen: a password field or a sign-in redirect = not signed in). **What the PC shows** fetches a
  screenshot of the agent's browser so a sign-in started from the phone can be followed. A row's recipe
  is its explicit `setup` block or is derived from the check type and the renew text. The engine never
  imports a secret store; nothing sends. CONNECTIONS rides in the brain's context so Sage names the page
  instead of asking for a password. `/setup` in chat.
- **The library (`resources.py`).** The chat page's paperclip (POST `/api/agent/upload`, same-origin,
  30 MB) and Google Chat documents land in `Raw/Uploads/YYYY-MM-DD_<name>` (git-committed), Office files
  get a `.txt` extract, `assistant/data/uploads.jsonl` records who/when/note, and the brain gets the same
  message: read it, answer, file it as a `note` with `"sources"` (wiki_writer now writes real `sources`).
  Every Raw/ file the wiki does not cite is already the nightly ingest's queue. Executables and footage
  refused. `/library`.
- **`assistant/new_org.py`** scaffolds a new organization's folder from this one: code only (no data,
  vault, tokens, rules, memory, chat), a generic `prompt.md` (`templates/prompt.generic.md`), profile /
  people / org.json seeded with the owners, generic + per-owner connection rows, a README checklist.
  Tests: `assistant/tests/test_org_setup.py` (71 checks).

## Using this as a template for another household or a company

The code holds no names, no business facts and no household rules. To stand up a new agent:

1. Copy `assistant/agent/` (the `.py` files and an empty `state/`).
2. Rewrite `profile.json` (name, who it serves, caps), `people.json` (who it answers and
   what each role may do), `prompt.md` (what it can read and how it should talk) and
   `google.json` (accounts).
3. Add the organisation's own never-self-edit topics to `profile.json` → `extra_protected`
   (a regex), the way example's profile protects "what may be posted without a tap".
4. Point `task` at that organisation's approval queue (`actions._queue_task`) and
   `wiki_writer.FOLDERS` at its knowledge base layout.
5. Stand up its own Chat app and OAuth client. Never share credentials between agents.

What does not transfer and must be decided per client: who may approve what, which actions
are reversible enough to auto-apply, and the protected core.

## Doing, not describing: attachments, Resolve, CRM replies, evidence (2026-10-15)

the owner, 2026-10-15, after three asks stalled in one morning ("reply in the CRM to Delia", "use
Corwin's new audio in the beach timeline", "build everything you need so you can do stuff like
this in the future"): "fix Sage so it can do the last few things I requested and when it has
evidence of completion report back". What changed, all in code around the same read-only brain:

- A CRM reply to a couple is a real send now: the operator cards it using the CRM connector's
  message kind (threaded on the couple's last message) and the CRM sends it
  from the owner's account on his tap. `prompt.md` rung 4 says so; "paste it into the CRM yourself"
  is gone from the operator prompt.
- Email attachments and the Google Drive links Gmail puts in a body are fetched to this PC by the
  computer-lane doer with `assistant/scripts/mail_attach.py`; audio on a Resolve timeline is
  swapped with `editor/resolve_swap_audio.py` (Resolve is launched when closed). `prompt.md` rung 4
  routes both as `do` with `where: computer`; `mail_read` now lists a message's attachments and
  Drive links with the fetch command.
- Every computer-lane DONE comes back with code-verified evidence (`computer.verify_evidence`:
  re-stat + re-hash of each file the turn claims, each check relayed), and every send that drain
  finishes posts its record (Message-ID / CRM message id + time) in the card's own thread.
- The doer's self-learning had been dead since 10-12 (`import learn` picked the wrong module);
  fixed in `doer._learn()`.

## Not built yet

- ~~Native Chat card buttons for approvals.~~ Built 2026-10-05: every card is mirrored into each owner's DM with
  a button per option (`assistant/scripts/chat_push.py`); a tap reaches Mission Control's `/gchat/events` over
  Tailscale Funnel (Google-signed token verified, `scripts/gchat_events.py`) and is answered on the spot through
  `cards.answer`; the reply is the updated card, Undo stays for 30 s. (Pub/Sub never delivers taps; see
  GOOGLE_SETUP.md 11b.) Mission Control still
  has the same card with the edit pencil.
- Mail and calendar sensing that starts a conversation by itself (a new email becoming a
  message from Sage). Today it reads when asked or when a schedule tells it to look. (New quiz
  leads DO start one since 2026-10-05: the sensor posts a "Hot lead - what next?" card.)
- Web search. Left off on purpose until an owner asks for it: anything the model reads can
  try to steer what it learns.

## Blocked work becomes the owner's portal (the owner, 2026-10-20)

"Anything that could not be done should not be put on an infinite loop; it should be modified so Google Chat acts as a portal for the user to do it themself as easily as possible." Held in four places: rule r67 in `state/rules.json` and rung 6 of `prompt.md` (and the generic template); the doer's `DOER_BLOCKED ... || DO IT YOURSELF: <url> then <phone steps>` hand-over rendered by `doer.portal`; `commitments.line_for`, which says a slipped build or job ONCE (`MAX_REPORTS = 1`) and offers the link-and-steps instead of "pushing it through again"; and `cards.sweep`, which dismisses a doer-blocked ticket instead of flipping it back to `new` for the doer to refuse again.
