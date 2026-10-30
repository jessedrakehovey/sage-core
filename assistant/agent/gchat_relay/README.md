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
