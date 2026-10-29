# Setting up Sage's Google connections: the runbook

Written 2026-10-05 from the setup actually done that day, with everything that went wrong
and the fix for each. Follow it in order to rebuild Sage on a new machine or to stand up
a new agent for another household or company. Time: about 90 minutes end to end the first
time, mostly waiting for Google.

Two Google Cloud projects, on purpose:

| Project | Owner account | Holds | Why separate |
|---|---|---|---|
| `example-agent` (id `vibrant-grammar-510717-e2`) | owner4@example.invalid | OAuth client for **calendars + mail** (3 consents) | the owner chose his personal account for this |
| `sage-chat` (id `x9efb7617dd5e0aa5b3f32f1`) | owner3@example.invalid (Workspace) | the **Chat app**, its service account, Pub/Sub, and the **space reader** OAuth client | Chat apps can only be built and used from a Workspace account |

Files that come out of it, all under `C:\Users\the owner\.example-mail\google\` (the personal
side's credential folder; never in the repo, never printed):

| File | What | Made in |
|---|---|---|
| `client_secret.json` | OAuth client, calendars + mail | Part 1 step 4 |
| `the owner-personal.json`, `the owner-films.json`, `owner.json` | one token per mailbox | Part 1 step 6 |
| `agent_sa.json` | the Chat app's service account key | Part 2 step 6 |
| `client_secret_chat.json` | OAuth client, space reader | Part 3 step 1 |
| `chat_reader.json` | the reader's token (owner3@example.invalid) | Part 3 step 2 |

Plus `assistant/agent/gchat.local.json` (git-ignored):
`{"project_id": "<chat project id>", "subscription": "chat-app-events-sub", "sa_key": "~/.example-mail/google/agent_sa.json"}`.

Which clicks Claude can do and which are the owner's: Claude can create projects, enable
APIs, fill forms, create service accounts and OAuth clients, download key files (with
permission) and configure the Chat app. **The owner does:** every Google sign-in and consent,
any organisation-policy change, and (as it turned out) IAM role grants, which the safety
check refused once and allowed once the owner said "do everything".

---

## Part 1: calendars and mail (owner4@example.invalid)

1. **console.cloud.google.com** → New project `example-agent`. Google assigns a random id
   (`vibrant-grammar-…`); read it from **Manage resources**, not from the project name.
   Gotcha: a URL with `?project=example-agent` silently opened *someone else's* project of that
   name and showed "You need additional access".
2. **Enable APIs:** Gmail API, Google Calendar API.
3. **Google Auth Platform → Get started:** app name `Sage`, support email, audience
   **External** (two of the three accounts are gmail.com), contact email, agree to the user
   data policy (owner's agreement; the owner approved it in chat), Create.
4. **Clients → Create client → Desktop app** → Download JSON → save as `client_secret.json`.
   Gotcha: the browser pane saved the download as a `.tmp` with a random name in Downloads;
   identify it by its first bytes (`{"installed"`) and move it.
5. **Publish the app** (Audience → Publish app). Left in "Testing", every token dies after
   7 days. Google will not publish without a **home page URL, a privacy policy URL and an
   authorized domain** on the Branding page; we used `https://www.example.invalid`,
   `/privacy-policy` and `example.invalid`. Status must read "In production".
6. **Consents**, one per mailbox, each run by the owner of that mailbox:

```bash
"C:\Users\the owner\.venvs\wedding-web\Scripts\python.exe" "{{ORG_ROOT}}\assistant\agent\google_api.py" auth the owner-personal
```

   (then `auth the owner-films`, `auth owner`). The script refuses a token whose signed-in
   address is not the one named, so a wrong-account sign-in cannot slip in. Expect Google's
   "unverified app" screen: Advanced → continue. Scopes are read-only mail, calendar
   read + add events; no send or draft scope exists on these tokens. **One exception
   (2026-10-18):** `the owner-films` also asks for `spreadsheets` (declared as `extra_scopes`
   in `google.json`) so the pricing sheet can be edited by API instead of the doer's
   browser; cells are written only by `drain.py` running a `sheet_set` ticket the owner
   approved on a card. An account whose token predates a new scope keeps working on the
   scopes it was granted; re-run `auth <name>` once to add the new one
   (`google_api.py scopes <name>` shows granted vs asked).
7. Check: `python assistant/agent/google_api.py status`, then ask Sage about the calendar.

## Part 2: the Chat app (owner3@example.invalid)

1. Sign in to the console as the Workspace account (the pane may need "Use another
   account"; a sign-in done in a different browser does not carry over).
2. **New project** `sage-chat`. It lands inside the example.invalid organisation. No billing
   account was asked for at any step.
3. **Enable APIs:** Google Chat API, Cloud Pub/Sub API.
4. **IAM → Service accounts → Create** `sage-chat` (no roles at creation).
5. **Organisation policy, owner's clicks (first wall).** Creating the key fails with
   "Service account key creation is disabled" (`iam.disableServiceAccountKeyCreation`, a
   Google default on new organisations). Fix, for this project only:
   - Organisation IAM page → Grant access → the owner's address → role **Organization
     Policy Administrator** → Save (even a super-admin lacks this by default).
   - Project → IAM & Admin → Organization Policies → `iam.disableServiceAccountKeyCreation`
     → Manage policy → Override parent's policy → Add a rule → Enforcement **Off** → Set
     policy. Takes a few minutes to apply; retry the key after that.
6. **Service account → Keys → Add key → JSON** → save as `agent_sa.json`.
7. **Pub/Sub → Create topic** `chat-app-events` with "Add a default subscription" ticked
   (that makes the pull subscription `chat-app-events-sub`).
8. **Chat API → Configuration:** app name, avatar URL (an https image; we used Google's
   quickstart avatar), description, "Join spaces" on, **Cloud Pub/Sub** with topic
   `projects/<id>/topics/chat-app-events`, visibility = the owners' Workspace addresses,
   Save. Gotcha: the "Build as a Workspace add-on" box re-ticks itself on save and cannot be
   changed afterwards. In add-on mode Google delivers through the **add-ons service agent
   shown on this page** (`service-<app number>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`),
   not through `chat-api-push`.
9. **Organisation policy, owner's clicks (second wall).** Granting a role to that Google
   account fails because of `iam.allowedPolicyMemberDomains` (Domain restricted sharing,
   another default). Same procedure: Override parent's policy → Add a rule → **Allow All**
   → Set policy, on the `sage-chat` project only.
10. **IAM grants** on the project (owner's clicks, or Claude with the owner's explicit
    go-ahead):
    - `sage-chat@<id>.iam.gserviceaccount.com` → **Pub/Sub Subscriber**
    - `service-<app number>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com` → **Pub/Sub Publisher**
    Note the Subscriber role cannot *read* subscription metadata; the health check uses a
    pull with no acknowledge instead.
11. Write `gchat.local.json`, then `python assistant/agent/gchat.py --test` → `OK`.
11c. **Buttons with no public address (sage-core, 2026-10-28).** An organization without Tailscale runs the same
    Apps Script relay in **pull mode**: no ENDPOINT property, a second deployment as a Web app, and on the PC
    `sage relay secret` → `sage relay url <web app url>` → `sage relay check`. Steps in
    `assistant/agent/gchat_relay/README.md` ("Pull mode"). **This machine runs pull mode since 2026-10-28 12:28**
    (the owner: "Yes, switch example"): the add-on deployment is version 6, the web app deployment "pull mode: web app for
    the PC poller" (URL in `gchat.local.json` → `relay_webapp_url`), the ENDPOINT property deleted, the poller the
    scheduled task "the organization" (`assistant/tasks/RelayPoll.xml`, every minute). First live taps:
    the Relay test card 12:30:30, an Edit & send dialog + submit 12:59-13:00. Rollback = put ENDPOINT back
    (Funnel :8443 is still up). 11b below is the push mode it ran before.
11b. **Buttons (2026-10-05).** A Pub/Sub app never receives button taps on its cards (every tap
    showed "could not process request"; zero events reached the subscription). Taps need a
    synchronous HTTPS reply, so the app's connection is switched to an **HTTP endpoint**:
    - Mission Control serves `POST /gchat/events` (`mc_server.py` → `scripts/gchat_events.py`),
      made public ONLY on that path through Tailscale Funnel:
      `tailscale funnel --bg --https=8443 --set-path /gchat http://127.0.0.1:9019/gchat`
      (from Git Bash set `MSYS_NO_PATHCONV=1` first or the path gets mangled). Everything else
      on Mission Control stays tailnet-only on :9443.
    - `gchat.local.json` gains `"project_number": "<the number in service-<n>@gcp-sa-gsuiteaddons>"`
      and `"endpoint_url": "https://<this-pc>.<tailnet>.ts.net:8443/gchat/events"`. Every event's
      Google-signed token is verified against them (issuer `chat@system.gserviceaccount.com`).
    - Chat API → Configuration → Connection settings: choose **HTTP endpoint URL** and paste
      that URL, Save. From then on messages arrive there too (queued in
      `assistant/data/agent/gchat_inbox.jsonl` for the bridge, which still replies by REST);
      the Pub/Sub pull keeps running and simply sees nothing.
    - **Check it actually happened (2026-10-06):** until the console switch is saved, taps still
      show "could not process request" and `assistant/data/agent/gchat_inbox.jsonl` never appears
      (messages keep arriving by Pub/Sub). A refused event now logs the token's unverified
      iss/aud next to the expected audiences, so an audience mismatch is visible at once.
    - **Port 8443 is the blocker (found 2026-10-06).** With the console correctly on HTTP endpoint
      URL, every tap still failed in ~3 s with Google's log saying "the Chat app didn't respond",
      and NOT ONE request reached Mission Control. Yet Google's own Sheets fetcher reached
      `:8443/gchat/ping` fine, as did Anthropic's fetcher: the port is public, but the Chat
      backend only calls port **443** (the one working Funnel+Chat setup in the wild puts the
      webhook on 443 and keeps its private UI on 8443). Port 443 on this node is the
      tailnet-only serve for the property-side apps (/, /cam, /walk, /voicegw), and Funnel is
      per port, so moving /gchat to 443 means those apps move to another tailnet-only port
      first. That is the owner's call (property side); nothing here is changed until he decides.
      `GET /gchat/ping` logs every hit (source IP + user agent) for reachability tests.
    - **The fix that shipped (2026-10-06 14:22): the Apps Script relay.** Nothing on this machine
      moved. Chat API → Configuration → Connection settings is **Apps Script**, deployment ID in
      `gchat.local.json` (`relay_deployment_id`); the script (`assistant/agent/gchat_relay/`,
      the owner's Drive, bound to GCP project 555-032-5432) forwards every event to the 8443 endpoint
      with `X-Sage-Relay: <relay_secret>`. Button taps arrive as `card_answer` / `card_undo`
      calls with no `invokedFunction`, so `gchat.parse_click` infers the function from the
      relay stamp or the button parameters. First successful tap: 14:21:59, "click answer … ok=True".
      Manifest must be the add-on shape (`addOns.common` + `addOns.chat`), or Chat logs
      "Broken manifest file for deployment" and the script never runs.
    - Proof: `curl -X POST -H "Authorization: Bearer nope" <url>` → `{"error":"unauthorized"}`;
      a real tap → `~/.claude/mission-control-wv.log` "click answer q…" and the card in Chat
      turns into its answer with an Undo button.
12. Each owner opens Google Chat as their Workspace account, New chat → **Sage**, says hello.
    A gmail.com account cannot see the app; owner uses owner6@example.invalid.
13. Restart the bridge (`bridge.py --stop`; the spine restarts it) and watch
    `~/.claude/agent-bridge.log` for `answered … on gchat`.

## Part 3: the shared-space reader and voice notes (owner3@example.invalid)

A Chat app is only told about DMs and @mentions. To read a shared space the way example does,
one member's own sign-in reads it (read-only scopes), and that same sign-in downloads
attachments.

1. In `sage-chat`: **Google Auth Platform → Get started**: app name `Sage reader`, audience
   **Internal** (no verification, no publishing, no warning screen), contact email, agree,
   Create. Then **Clients → Create client → Desktop app** → Download JSON → save as
   `client_secret_chat.json`.
2. Consent, run by the owner as owner3@example.invalid:

```bash
"C:\Users\the owner\.venvs\wedding-web\Scripts\python.exe" "{{ORG_ROOT}}\assistant\agent\space_reader.py" auth
```

3. Check: `python assistant/agent/space_reader.py spaces` lists the spaces the app is in.
   Messages older than the reader's start are not answered; wind the watermark back in
   `assistant/data/agent/space_watermarks.json` if they should be.
4. Voice notes need `~/.venvs/whisper` with faster-whisper (already on this PC) and ffmpeg.

## Part 4: the agent itself (no Google)

- **Claude account:** `profile.json` → `claude_account` names a token in the machine's
  DPAPI store `~/.claude/accounts.dpapi.json` (written by the property side's account
  switcher). Sage runs on `max5x`. The safety check refused the first attempt at wiring this
  as credential access; it went through on the owner's explicit go-ahead.
- **Spine jobs** (`assistant/spine_jobs.json`): `agent-bridge` (every 10 min, keepalive) and
  `agent-dream` (06:00–07:00). The bridge must be started by the spine or a terminal, never
  from inside a Claude session (the session scrubs the auth token from its children).
- **Windowless:** every child process uses `CREATE_NO_WINDOW`; the loop runs under
  `pythonw.exe`. the owner saw command windows pop up before this was done.
- **Single instance:** an OS lock in `bridge.py`; two spine passes once started two bridges.
- **Gate baseline:** `scripts/guardrails/check_gates.py --freeze` after any change to
  `brain.py`'s tool lists.
- **Mission Control chat page** `/chat` is the fallback channel and needs nothing from Google.

## Troubleshooting seen on day one

| Symptom | Cause | Fix |
|---|---|---|
| Sage never answers in Chat, queue empty | no Publisher grant, or granted to `chat-api-push` instead of the add-ons agent | Part 2 steps 8–10 |
| `HTTP 403` on the queue | Subscriber grant missing, or the check used subscription GET | grant; the check now pulls without ack |
| `HTTP 401` on the queue after ~1 h | token expiry read as local time | fixed in `gchat.py` (`replace(tzinfo=utc)`) |
| Audio message ignored | events with no text were dropped | fixed; attachments go through the reader |
| Messages in a space ignored | no @mention; the app only gets mentions | the space reader (Part 3) |
| "Sage not responding" in Chat | bridge down, or the message predates the reader | `bridge.py --status`; wind back the watermark |
| Two bridges answering | keepalive race | single-instance lock |
| Command windows on screen | console children of a detached loop | `CREATE_NO_WINDOW` everywhere |

## Drive to wiki (added 2026-10-05)

`scripts/drive_ingest/` reads both of the owner's Drives with its own read-only token per account
(`~/.example-mail/google/<name>.drive.json`), using the **example-agent** OAuth client.
- The **Drive API must be enabled on example-agent** (`vibrant-grammar-510717-e2`, owned by
  owner4@example.invalid). On day one it was first enabled in sage-chat by mistake, because the console was open as the
  example account. Use `...apis/library/drive.googleapis.com?project=vibrant-grammar-510717-e2&authuser=owner4@example.invalid`.
- Connect: `python scripts/drive_ingest/drive_ingest.py auth the owner-personal` and `... auth the owner-films`.
  The signed-in address is read from the consent's id_token. A network timeout on the old userinfo
  check once threw away a consent the owner had just given.
- `drive_ingest.py status` shows the connections, watermarks and pending files. The task is "example Drive to Wiki".
