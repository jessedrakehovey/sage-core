---
name: sage-setup
description: Stand up a Sage agent for an organization on this computer, from nothing - a co-pilot install. Claude installs what is missing (git, Python, Node, Playwright, gcloud, the Claude Code CLI), gets the sage-core engine, creates the organization's pack, starts the supervisor and Mission Control, walks the owner through the Google Workspace clicks that are theirs alone, then hands off to Discovery and Training in chat. Trigger phrases - "set up Sage", "install Sage", "sage setup", "I want my own Sage", "new Sage agent for <org>", "/sage-setup".
---

# sage-setup: a Sage agent for this organization, from a bare machine

You are setting up an **employee**, not a script. The owner should finish this with an agent that answers them
in Google Chat, drafts everything, sends nothing without their tap, and already knows how to learn their business.
Say in one line what you are about to do before each step, and report each step's check as it passes. Never
paste a token, password or key into a message, a file or a chat; the owner types those where the step says.

## What you can do and what the owner does

- **You do:** every install, the engine download, the pack, the supervisor, Mission Control, the Cloud project,
  enabling APIs, service accounts, OAuth clients (the forms), the Chat app configuration, every check.
- **The owner does (always, no exception):** every Google sign-in and consent screen, `claude setup-token` (the
  token is their subscription), IAM grants when the console asks for them, any organization-policy change,
  pasting an app password. Give them the exact link and the exact clicks, one step at a time, and wait.
- **Google Workspace is required** for the Chat surface (a Chat app cannot be built on a consumer Gmail). If
  they have none, send them to workspace.google.com (about $7 per user per month, a domain, DNS verification that
  can take a day) and continue with the steps that do not need it; Mission Control and the terminal chat work meanwhile.

## 0. Before anything: is the installer the better path?

An owner who does not want a terminal gets `SageSetup.exe` (`assistant/installer/README.md`): it installs a private
Python, the engine release, the browser and the Claude CLI, then opens the setup page (`assistant/welcome.py`) that asks
the same three questions, creates the pack, shows the doctor table, opens the token window and starts the agent. Use this
skill when there is no installer build for their machine, when something in that flow failed, or when the owner wants a
copilot. Both end in the same pack and the same `sage update`.

## 0b. The engine and the pack location

Ask three things, nothing more: the organization's name, **what they want to call the agent** (their choice, asked every
time, never defaulted: it is their employee's name), and each owner as "Full Name:email". The engine is `sage-core`;
the agent is whatever they name it. Decide the engine source: `$env:SAGE_ENGINE` / `$SAGE_ENGINE` (a folder or a git URL), else
ask for it. The pack goes in a folder of its own next to nothing else (default `~/Documents/<Org> OS`).

## 1. Preflight and installs (bootstrap)

Run the bootstrap for this OS from this skill's folder; it is idempotent and prints what it did:

```powershell
powershell -ExecutionPolicy Bypass -File "<this skill folder>\bootstrap.ps1" -Org "<Org>" -Agent "<Agent>" -Owner "Name:email" [-Owner "Name:email"] -Engine "<folder or git url>" -Out "<pack folder>"
```

```bash
bash "<this skill folder>/bootstrap.sh" --org "<Org>" --agent "<Agent>" --owner "Name:email" --engine "<folder or git url>" --out "<pack folder>"
```

It installs git, Python 3.11+, Node LTS, the Claude Code CLI, gcloud (winget / brew / apt), makes the venv
`~/.venvs/<org-slug>-sage` with `assistant/requirements.txt`, installs the Playwright Chromium browser, clones or
copies the engine, runs `new_org.py` (the pack), writes the venv into the pack's `assistant/org.json -> python`,
and ends with `python assistant/sage.py doctor --no-probe`. Read the doctor table. Every FAIL has a fix line.

## 2. The Claude token (the owner's, minted here, never copied)

Headless runs cannot use the desktop app's sign-in. In a **fresh terminal** (not inside this session) the owner runs
`claude setup-token`, then stores the result as the User env var `CLAUDE_CODE_OAUTH_TOKEN`
(Windows: `setx CLAUDE_CODE_OAUTH_TOKEN "<token>"`; macOS/Linux: in their shell profile). If the organization bills
by API instead, `ANTHROPIC_API_KEY` with a monthly cap, and `assistant/org.json -> claude_billing = "api"`.
Never both. Then, again in a fresh terminal: `python assistant/sage.py doctor` (the headless probe must say OK;
inside a Claude session it reports "Not logged in", a false negative, so do not run the probe from here).

Tell the owner plainly: the agent's runs count against their own subscription rate limits.

## 3. The supervisor and Mission Control

```bash
python assistant/sage.py serve
```

runs the spine, the probe, the operator, the deadman and Mission Control in one process. In a second terminal:
`python assistant/sage.py status` (serve running, Mission Control up at http://127.0.0.1:9019), then
`python assistant/sage.py install-startup` so it starts at logon (schtasks / launchd / systemd --user). Open
Mission Control, answer the first card (the smoke test), show the owner `python assistant/sage.py pause` and
`stop`: they must know how to turn it off before anything else is connected.

## 4. Google, in this order

Follow `assistant/agent/GOOGLE_SETUP.md` in the pack (generic copy). Use `gcloud` for projects, APIs, the
service account and Pub/Sub; give the owner one link per click that is theirs:

1. Workspace confirmed (or signing up; continue meanwhile).
2. Cloud project for mail + calendar; APIs enabled; the OAuth consent screen (owner: the sign-in and consent);
   the token per mailbox lands in the pack's `secrets_dir` (org.json), never in the repo.
3. Cloud project for Chat (a Workspace account); the Chat app; Pub/Sub topic + subscription (messages arrive by
   PULL, nothing listens on the internet); the service account key into `secrets_dir`; `assistant/agent/gchat.local.json`.
4. Buttons without Tailscale: the Chat connection is the Apps Script relay in **pull mode**
   (`assistant/agent/gchat_relay/README.md`, "Pull mode", about 15 minutes): `python assistant/sage.py relay secret`
   on the PC, the owner pastes `Code.gs` + the manifest into a new Apps Script project with the secret as the
   `RELAY_SECRET` property and NO `ENDPOINT`, deploys it twice (Web app + Add-on), pastes the Add-on deployment ID into
   the Chat app's connection settings, then `python assistant/sage.py relay url <web app url>` and `sage relay check`
   on the PC. That turns `config.json push.buttons` on; until then cards carry their choices as words.
5. The owner adds the agent to a DM or a space and says hello. `python assistant/agent/gchat.py --test` first.
6. Logins and contacts, from chat, with nothing secret in chat: an owner who says "save my Stripe login" (or pastes a
   password, which is refused and scrubbed by code) gets one card whose **Enter login** button opens a Chat dialog;
   the fields go to the agent's vault on the PC, never into the conversation. A contacts export (.vcf) shared into
   the DM is imported for that owner and the copy deleted. Tell the owner both; never ask them to type a password
   in a message.

## 5. Discovery, Training, the shadow week, go-live

Now the agent learns the business. Run these with the owner at the keyboard (or in chat once Chat is live):

- `python assistant/onboarding.py discover`: one question per business function (leads, CRM, calendar, mail, text,
  payments, contracts, scheduling, files, team chat, accounting, social, automation, banking). The catalog picks
  the access tier for each tool; connect each on Mission Control `/setup`. `onboarding.py map` is the capability
  map: read it to the owner ("I can read X, write Y, Z always waits for your tap").
- `python assistant/onboarding.py train`: the lifecycle skeleton for their business type, six fields per stage
  (trigger, actor, channel, template, clock, verifier). Stop on `COVERAGE 100%`, not on a count. Ask from
  artifacts: when the agent can read their mail and CRM, confirm what it sees ("you replied within two hours with
  these words; is that the rule?") instead of asking in the abstract.
- `python assistant/onboarding.py never`: the never-list, seeded at maximum gating; the owner adds their own lines.
- `python assistant/onboarding.py shadow on`: for 5 to 7 days the agent drafts every reply, reminder and follow-up
  and sends nothing; the owner grades drafts in the weekly digest. Then `shadow off`.
- `python assistant/onboarding.py golive --enable <switch>`: each standing exception on by name, dated.
- `python assistant/onboarding.py blueprint` renders the whole thing into the wiki as a by-product.

## 5b. The organization's own documents (nothing automatic)

The pack starts with an empty `Wiki/`. The owner's existing material (procedures, price lists, contracts, checklists,
guides, transcripts; any type: md, pdf, docx, pptx) goes into `Raw/` (`Raw/Uploads/` is where chat uploads land; make
subfolders as they like). Nothing is ingested on its own: `python scripts/wiki_tool.py sources` lists what the wiki does
not hold yet (`Wiki/_meta/sources.md`), and the owner says **"ingest"** in a Claude session in the pack folder (the
`wiki-ingest` skill), which compiles a few at a time into `status: draft` notes per `Schema/schema.md` (the generic
contract `new_org.py` installed: types, tags and folder routing in machine-read fenced blocks the owner may extend).
`python scripts/wiki_tool.py audit` must end at 0 errors. Once `sage serve` runs, the 06:30 dreaming job takes up to 3
pending sources a night; until then the SessionStart hook says so plainly. Semantic search over the wiki is optional:
`pip install -r assistant/requirements-semantic.txt` into the interpreter `org.json -> semantic_python` names
(`sage doctor`, row "semantic search"); without it the keyword search works.

## 6. Hand-off

Finish with: `python assistant/sage.py health` (did / failed / needs you), the pack's `README.md` (its checklist),
and where the owner finds things: Mission Control, `sage pause|stop|uninstall`, `sage update --engine <engine>`
for a newer engine (pack files are never touched), and the engine's `README.md`. Record anything you had to work
around in the pack's `Schema/mistakes.md` so the next setup does not repeat it.
