# gchat_relay — the Apps Script pipe between Google Chat and Mission Control

## Pull mode: card taps with no public address (sage-core, 2026-10-28)

the owner: "Can we make approval cards work without tailscale for orgs in sage-core? Let's do that." The relay now has
two modes, chosen by its script properties. **Push** (ENDPOINT set) is everything below: events are forwarded to a
public Mission Control URL. **Pull** (no ENDPOINT) needs nothing public on the PC: each Chat event is queued in the
script's cache, the PC's poller (`assistant/scripts/relay_poll.py`, serve job `relay-poll`, every few seconds)
fetches it through the script's **web app URL**, hands it to Mission Control's own `/gchat/events` on loopback with
`X-Sage-Relay`, and posts the reply back; the Chat handler waits up to 22 s for that reply, so a tap still becomes the
answered card in place. A slow reply gets the optimistic "got it" card and Mission Control finishes by REST
(`X-Sage-Late`), exactly as v5 does for held taps. A **vault submit** (a password typed into the Enter login dialog) is
the one event never held: no reply within the wait and it is dropped with "nothing was saved".

Setting it up (the owner's Google account; about 15 minutes):

1. On the PC: `python assistant/sage.py relay secret` prints the shared secret once.
2. script.google.com → New project → paste `Code.gs`; Project Settings → Show `appsscript.json` → paste the manifest
   (it has the `webapp` block); set the GCP project number of the Chat project; Script properties: `RELAY_SECRET` =
   the secret from step 1. **No ENDPOINT property**: that is what selects pull mode.
3. Deploy → New deployment → **Web app** (execute as me, anyone) → copy the URL (`https://script.google.com/macros/s/…/exec`).
   Deploy → New deployment → **Add-on** → copy the Deployment ID.
4. Chat API → Configuration → Connection settings → **Apps Script**, paste the Deployment ID, Save.
5. On the PC: `python assistant/sage.py relay url <web app url>`, then `python assistant/sage.py relay check`: the
   relay answers, `config.json push.buttons` turns on, and `serve`'s `relay-poll` job carries the taps from then on.
   `python assistant/sage.py doctor` shows the row "chat relay".

Quota note: the PC polls every 3 s by default (`RELAY_POLL_INTERVAL`); on a Workspace account that is well inside
Apps Script's daily runtime. `installFlushTrigger` is push-mode only.

**This organization switched to pull mode on 2026-10-28** (done from the doer's signed-in browser over CDP, the
owner's sign-in the only hand step): add-on deployment `AKfycbxtOfRX…` at version 6, web app deployment "pull mode:
web app for the PC poller", ENDPOINT deleted, poller = scheduled task "example-org Relay Poll (assistant)". Lessons from
the switch: the doer closes the shared browser at the end of its runs (hold `doer-tick` while driving the IDE); a
new deployment made with "Web app" ticked can still come out Library-only, so re-open it in Manage deployments and
check the web app URL is shown; Apps Script's cold start answers in 20-40 s, so the poller's read timeout is 45 s.

## Push mode (the original, 2026-10-06)

Why (2026-10-06): Google Chat's HTTP connection only calls port 443. This machine's only
public path is Tailscale Funnel port 8443 (port 443 is the property-side tailnet-only serve
and must not change). Google's other fetchers reach 8443 fine; Chat does not. So the Chat app's
connection is an **Apps Script deployment** of these two files, and Apps Script forwards each
event to `https://<this-pc>.<tailnet>.ts.net:8443/gchat/events` with `X-Sage-Relay: <secret>`.
`mc_server.py` accepts that header (constant-time compare against `relay_secret` in
`assistant/agent/gchat.local.json`) as an alternative to Google's signed bearer, which still
works too. Everything after the door is unchanged: `gchat_events.handle` → `cards.answer`.

Google side (owner's account, owner3@example.invalid; the script lives in the owner's Drive):
1. script.google.com → New project "Sage relay"; paste `Code.gs`; Project Settings → tick
   "Show appsscript.json", paste the manifest; set the GCP project number **5550325432**.
2. Project Settings → Script properties: `ENDPOINT` = the URL above, `RELAY_SECRET` = the
   value of `relay_secret` from `gchat.local.json`.
3. Run `pingTest` once (authorise) → log shows `200 {"ok":true,...}` and Mission Control's log
   shows `gchat ping from <google ip> ua=...Apps Script`.
4. Deploy → New deployment → type **Add-on** → Deploy; copy the **Deployment ID**. Later code
   changes: Deploy → Manage deployments → pencil → Version: New version → Deploy (same ID).
   The manifest MUST be the add-on shape in `appsscript.json` here; the legacy top-level `chat`
   key makes Chat log "Broken manifest file for deployment" and the script never runs.
5. Chat API → Configuration → Connection settings → **Apps Script**, paste the deployment ID, Save.
6. Tap a button on a Sage card: Mission Control log shows `gchat event via relay` then
   `click answer q…`, and the card turns into the answer with Undo.

## Leased events and the ack: a lost poll loses nothing (2026-11-04)

the owner tapped a setup button and Chat said "Unable to process request"; Mission Control never saw the tap. The
poller's read had timed out while Apps Script was slow (it happens a few hundred times a day), and `popEvents_`
had already REMOVED the events it was handing over, so the tap was gone for good and the relay's reply for an
unknown function was `{}`. Now `poll` LEASES events (`lease_<id>`, LEASE_S = 12 s) and the PC posts `op=ack` with
the ids once Mission Control took them (`relay_poll.py`, which also keeps `data/agent/relay_seen.json` so an event
handed over twice runs once); an un-acked event comes back on the next poll. The poller's read timeout dropped to
30 s. And every function has an optimistic reply: a setup button (`form_action`) shows "Got it, my computer is
picking this up" (Mission Control then puts the real working card up by REST when the late tap arrives), a form
dialog closes with "got it", `form_open` says busy. Deployed as add-on version 8 and web app version 10 (the web
app deployment carries `doGet`/`doPost`, so it must be redeployed too: `chat_console.py deploy` does both).

The second "Unable to process request" the same day was simpler: Apps Script's execution log said
`Script function not found: form_action`. The add-on runtime dispatches a tap to the function NAMED in the button,
and `form_open` / `form_submit` / `form_action` (chat_forms.py, 2026-11-01) had never been added to this file. Added,
add-on version 11. `tests/test_home_tab.py` now checks that every button function the PC's cards use is defined
here, so a new button name cannot ship without its relay function again. The executions page
(`script.google.com/home/projects/<id>/executions`) is the first place to look when Chat says that line.

## The Home tab and the registered commands (2026-11-04, Phase 2 of the desk window)

`Code.gs` gained `onAppHome` (the Home tab asks for its card) and `home_action` (every chip and button on
that card), both stamped and relayed like a tap; `slim_` now forwards `chat.appCommandPayload` (a registered
slash or quick command) and `onAppCommand` stamps itself. The PC answers all three from `scripts/home_tab.py`
with no model: `gchat_events.home_open` returns RenderActions `pushCard`, `home_click` returns `updateCard`
(or `updateMessageAction` when the same card was posted into a space by a command), `command_event`
answers `/desk /jobs /approvals /watch /stop /login /settings /help` with a card message and hands any
other registered command to the brain as a plain message. While the PC is slow the relay shows a
"slow to answer, tap Desk again" card (never held: NEVER_HELD_).

To turn it on: **tap "Set up the Home tab" in chat** (say "set up the home tab"; connection `chat-home`,
setup kind `job`). `scripts/chat_console.py` then does the Google clicks in the agent's own browser:
`deploy` pastes `Code.gs` into the Apps Script project (the Monaco model, not keystrokes) and deploys a
new version of the add-on deployment; `configure` ticks **Support App Home**, sets its trigger to
`onAppHome`, adds every row of `home_tab.COMMANDS` that is missing (ids 1-8 slash, 11-14 quick) and
saves. Idempotent. When Google re-asks the owner's password (the console does, every few hours) the run
stops with NEEDS_SIGNIN, puts the browser on the PC screen at that page (Control PC shows it from a
phone) and the card says to sign in there and tap again. Never types a password. `sage doctor` row
"chat home tab" reads `data/chat_console_state.json`. By hand: `<wedding-web venv> assistant/scripts/chat_console.py all`.
Done here on 2026-11-04 (add-on version 7, App Home on, 12 commands). In the Sage DM: the **Home** tab
shows the desk; the ⚡ menu shows the quick commands; `/desk` answers with the same card in the conversation.

## Edit & send (2026-10-06)

Every card whose approve would send text (email body, iMessage body, note) carries an
**Edit & send** button. In Chat it opens a dialog with the outgoing text prefilled; the owner
rewrites it and taps the approve button in the dialog. That submits `card_edit_submit`
(`commonEventObject.dialogEventType = SUBMIT_DIALOG`, text in `formInputs.body`), which
`gchat_events.click` turns into `cards.answer(approve_label, revised=<text>)`: the same
path as the pencil on Mission Control, so the ticket is minted with the edited text and the
30 s undo still applies. The relay needs `card_edit` and `card_edit_submit` functions
(both just stamp `relayFunction` and forward). Deployed as version 4.

## Hands-free texting (2026-10-06)

An approved iMessage used to wait until the owner opened the "example-org Send Texts" Shortcut. Now
`phone_bridge.queue_outbound` calls `phone_nudge.fire()`: one SMS-gateway email
(`config.json` -> `phone.nudge_to` = the owner's number @vtext.com, from the example account)
reading "example-SEND n text(s) ready". An iOS automation (Message contains example-SEND, Run
Immediately) runs the Shortcut, which collects the outbox. First live run: nudge 16:20:11,
phone collected the text 16:20:34, nobody touched the phone.

## v5: taps never fail (2026-10-19)

the owner: "sometimes I get Sage unable to process request on approvals but it works much later.
Maybe we should just make the buttons instantly work and then only tell me later if the
requests didn't work with a fresh approval." Mission Control answers a tap in well under a
second; the error came from taps landing while it restarted on new code (five restarts in four
minutes on 10-18 evening), and from the relay's fallback reply, which was not in the add-on
shape Chat accepts. Now:

- **Relay (this folder):** a tap or message that can't get through is retried 3 times over
  about 3 s. If the PC is still away, the event is **held** in Script Properties, and Chat shows
  the choice at once ("got it, goes through the moment Sage's computer is back").
  `flushQueue()` hands held events over (header `X-Sage-Late: <ms>`) after the next event
  that gets through, and every minute once `installFlushTrigger()` has been run once. Every
  fallback reply is now in the add-on shape.
- **Mission Control (`gchat_events.py`):** the work runs on its own thread. Done within 3 s:
  the reply is the exact outcome, as before. Not done: Chat gets the answered card right away
  and the thread finishes alone. A held tap is acted on only if it is under 2 hours old. Any
  failure (or a stale held tap) comes back as **the same card, fresh, with one line saying
  why** (`report_failed`). Undo and Edit & send held taps are handled the same way.
- **Mission Control restarts** only after its code has been still for 90 s, and it lets any
  tap still in flight finish first.
- **No error while it works (2026-10-25):** a slow tap's card shows ⏳ "Working on it" and flips
  to the real outcome by itself when it lands. A tap the relay delivers twice gets the card as it
  stands back, never an empty reply (Chat shows that as "unable to process", which is what
  the owner saw at 5:36 on Corwin's resend while it was really sending).

Deploying v5 (owner's account): paste `Code.gs` over the old code, add the scope
`https://www.googleapis.com/auth/script.scriptapp` to `appsscript.json` (already in the copy
here), Deploy → Manage deployments → pencil → New version → Deploy (same ID), then run
`installFlushTrigger` once in the editor and accept the permission prompt.

## 2026-11-04: no error of the relay's own ever reaches Chat

"Unable to process request" on a tap that then went through anyway was the script lock: `waitLock` throws when another run
holds it too long, and the exception became Chat's own error. The queue now takes its lock with `lock_()` (`tryLock`, never
throws; a run that cannot get it goes on without it), and `relay_()` catches everything and answers with the same reply as
when the computer is slow. `LEASE_S` is 12 s (a poll whose answer was lost no longer keeps a tap past Chat's wait). The
poller (`scripts/relay_poll.py`) waits 8 s per request instead of 30, asks again at once after a dropped poll, handles each
event on its own thread so the next poll is never kept waiting, and runs 56 s of every minute (it covers the next minute
too when a hand-over made it outlast its own).
