# Installing a Sage agent (sage-core)

Three ways onto a machine; every one of them is kept current by the same update path.

## 1. The installer (Windows, no terminal, no git)

`SageSetup.exe` installs per user, no admin, under `%LOCALAPPDATA%\Sage`:

- `python\`: the python.org embeddable Python 3.11 with pip and `assistant/requirements.txt` (private to Sage; the system's Python is never touched)
- `engine\`: the engine release (`engine_manifest.json`, `assistant/`, `scripts/`, `Schema/`, `.claude/`)
- the Playwright Chromium browser (`%LOCALAPPDATA%\ms-playwright`) and the Claude Code CLI by its native installer (`%USERPROFILE%\.local\bin\claude.exe`; skipped when a `claude` is already on the machine)
- `fonts\`: the app's two typefaces (in the bundle, else fetched once), so its window looks the same with no network
- `sage.ico` and a Start-menu and a desktop entry **Sage** that open the app
- `install.json`: where these are, read by the app

**One click, then an app (2026-11-14).** The installer asks nothing: one screen ("Welcome to Sage", Install), then "Setting up Sage" with the step it is on ("Step 3 of 6: Installing the building blocks"), a real bar and a download's percent. `install.ps1` runs with no window of its own and reports to the installer line by line (`##step N/M <title>`, `##fail <why>`); a failure is one plain sentence and "run the installer again, it picks up where it stopped", with the detail in the Setup log. Then **the Sage app opens in a window of its own** (`assistant/app.py`): this computer's Edge or Chrome (else the agent's own browser) in app mode with a profile of its own, so there is no address bar and no tab, its own icon in the taskbar, and a second click on the Start-menu entry brings the open window to the front. The window shows `assistant/app_ui.html` from a service on 127.0.0.1 at a port the system picks; every call carries a key made at start, so no other page can drive it; it loads nothing from the network.

Inside the app, the first time: a welcome, the organization's name, **what to call the agent**, the owners (name + email; the folder defaults to `Documents\<Org> OS` under "Where the files live"), "Setting up <agent>" (`new_org.py`, then the checks), this computer (the doctor table in plain words: only what it needs now, with a **Fix** button for what the app can install itself; the rest belongs to later steps), **Connect Claude** (one button: a small window runs `sage token --auto`, the browser asks for the sign-in, the token is read off that window and stored as the User environment variable `CLAUDE_CODE_OAUTH_TOKEN` with no copy and paste, and the window closes; the paste prompt remains the fallback), the Google steps (links open in the owner's own browser, where they are signed in), and then **home**: the agent running or paused, Pause / Resume, the folder, what is connected and what comes next. The app starts the supervisor (`sage.py install-startup`: at every logon from then on). Once an organization exists the Start-menu entry runs the organization's own copy of the app, which the hourly update keeps current. `python assistant/app.py --no-window --dry` serves the window for a look without starting anything; `--quit` closes it (the installer and the uninstaller ask this first).

**Connect Google, with one sign-in (`assistant/google_connect.py`, 2026-11-14).** The app's Google screen replaces the hour in Google's consoles. One button: Google's own command-line tool is fetched from Google into the app's folder (54 MB, run on the private Python, its settings and sign-in kept in `google-setup\config`, apart from any the owner already has), the owner signs in to Google once in their browser, and the app does every part Google lets a program do: a Cloud project inside the organization the owner signed in with, the services (Chat, add-ons, Pub/Sub, Gmail, Calendar), the agent's own identity and its key file (into the organization's keys folder), the message line and the agent's right to read it, `agent/gchat.local.json`; then the sign-in is handed back (`auth revoke`), so no owner-level Google credential stays on the computer. The window shows each step as it runs. Three things stop it with a plain sentence and one button: a personal Gmail account (Chat apps need Workspace: "Use a different Google account"), Google Cloud's terms not accepted yet, and **Google's rule against new keys**, which every new organization has: the app ASKS ("Allow one key for <agent>?"), and only on Allow lifts that rule for the agent's project alone, never the organization. A run that stopped picks up where it stopped (`data/google_connect.json`).

What Google offers only as pages stays by hand, guided, with a Copy button for every value and a link to the right page of THIS project: the sign-in screen, the relay script (code, manifest, project number, the shared secret, two deployments whose address and ID are pasted back and checked before they are saved), and the Chat app's page (six fields). "Check the connection" then proves the line with the agent's own tests (`gchat.py --test`, `sage relay check`), marks Google Chat connected and removes the setup tool. Proven: the tool downloads and runs on the installer's Python; every step against a stand-in for Google's tool (`tests/test_google_connect.py`, which also asserts what is NOT asked of Google without the owner's Allow). **Run live once, part of the way (2026-11-15, a Workspace owner on a throwaway agent, stopped at the owner's word):** the tool's download, the sign-in, a project inside the organization, the services, the agent's identity, the stop at the rule against new keys, and the owner's Allow lifting that rule for the one project all worked against real Google. That run is where the wait for a new identity, the retry of a stalled key request, the half-hour sign-in, "join the run that is going" and the least-change Allow came from. **Still unproven live:** the key file itself after an Allow, the message line, handing the sign-in back, the guided relay and Chat page, and "Check the connection".

The browser set-up page (`assistant/welcome.py`, `sage welcome`) remains for a clone install and at `/welcome` on the agent's local page; the installer no longer opens it. The steps are the same code (`welcome.create`, `doctor`, `start`).

Uninstall (Settings → Apps → Sage) closes the app, stops the supervisor, removes the logon entry, `python\`, `engine\`, `bootstrap\`, `fonts\` and the app window's profile. **The organization's folder is never deleted**; `sage.py uninstall` prints what else is the owner's to revoke (the token, the Google clients).

Build it (needs Inno Setup 6, a user install: `winget install JRSoftware.InnoSetup --scope user`):

```powershell
powershell -ExecutionPolicy Bypass -File assistant\installer\windows\build.ps1 -ReleaseUrl https://<host>/<path>
powershell -ExecutionPolicy Bypass -File assistant\installer\windows\build.ps1 -ReleaseUrl https://<host>/<path> -Bundle   # carries everything
```

**`-Bundle` (2026-11-14): one file, nothing fetched but the Claude CLI.** Without it `SageSetup.exe` is a few MB and downloads Python, the packages and the browser while it installs. With it they ride inside the installer (`dist\bundle\`: the embeddable Python zip checked against the pin in `install.ps1`, `get-pip.py`, every wheel, the Playwright browser folders) and `install.ps1` installs from that folder with pip's index switched off. The build proves the bundle before it ships: it unpacks the bundled Python, lets THAT Python pick the wheels (the build machine's own Python reads the "only below 3.12" conditions for itself and leaves wheels out), installs from the bundle into a scratch folder, and has that install fetch the browser, so it is the build the bundled Playwright expects. The Claude Code CLI is the one piece still fetched at install time, from its maker: it is not ours to hand out, and it needs the internet to work at all. `install.ps1 -Bundle <folder>` does the same by hand; `-PythonOnly` stops after Python and pip.

`build.ps1` runs `assistant/release.py build` (the zip + `release.json` into `dist/`), `make_assets.py` (the icon and the installer's corner image, drawn by code: the repository carries no picture files) and `ISCC` on `SageSetup.iss`; the result is `dist\SageSetup.exe`. `SageSetup.iss` sets `RedirectionGuard=no`: Setup's guard against folder links is inherited by everything it starts, and pip then refused a package cache the owner had moved to another drive (the installer runs as the user, with no elevation, so the guard holds no boundary here; `install.ps1` also runs pip with no cache). A test run: `SageSetup.exe /DIR=<folder> /InstallArgs="-SkipBrowser -SkipClaude" /LOG=<file>`. `-ReleaseUrl` is where the release files will be served from and is written into the installer so every organization it installs knows where to look for updates; it defaults to the checkout's GitHub origin as `.../releases/latest/download` (the engine itself carries no account name). Publishing is one command from the engine checkout, with the GitHub CLI signed in:

```powershell
powershell -ExecutionPolicy Bypass -File assistant\installer\windows\release.ps1      # build, tag v<version>, `gh release create` with the zip, release.json, .sha256 (+ SageSetup.exe when Inno is installed)
```

`latest/download/<file>` on GitHub always points at the newest release, so installed organizations need no version in their URL: the nightly job (`serve.json` `engine-update`) finds a new release by itself, checks its sha256, gates it and swaps it in. **The repository must be public** for that download to work without a token; while it is private, `sage update` says "no public release yet" and installs update by hand. A clone-based install derives the same feed from its origin; the copilot skill goes through `new_org`, so it gets it too. `install.ps1` alone (the installer's worker) also runs by hand:

```powershell
powershell -ExecutionPolicy Bypass -File assistant\installer\windows\install.ps1 -EngineZip dist\sage-core-<version>.zip -ReleaseUrl https://<host>/<path>
```

## 2. A git clone (anyone comfortable with a terminal)

```bash
git clone <sage-core repo> sage-core
python sage-core/assistant/new_org.py "<Org>" --agent <Name> --owner "Full Name:email" --out "<folder>"
cd "<folder>" && python assistant/sage.py doctor && python assistant/sage.py serve
```

`python assistant/sage.py welcome` opens the same setup page from a clone.

## 3. The copilot skill (Claude Code on the machine)

`assistant/setup_skill/sage-setup/`: `/sage-setup` in a Claude Code session installs what is missing, creates the pack and walks the owner through the Google clicks; `bootstrap.ps1` / `bootstrap.sh` do the installs. This is the support path: it ends in the same pack, the same supervisor.

## Several agents on one computer (`assistant/coexist.py`, 2026-10-30)

A second organization on a PC that already runs one never shares what the first one uses. `new_org.py` asks `coexist.allocate`
for free values and writes them into the new pack: its own Mission Control port (9020 up; 9019 is the first one's, 9018 the
setup page), its own doer browser port (9334 up) and profile folder, and `org.json state_home` = `~/.sage/<slug>`, where its
logs, Chat-bridge lock, Claude account store, builder worktree and run-budget counter live (`orgcfg.state_env`; a pack
without `state_home` keeps `~/.claude` exactly as before). The new pack's watched tasks carry its own name, it refuses a
name or a folder another pack already has, and it is listed in `~/.sage/packs.json`. `sage.py serve` and `install-startup`
refuse to start while `python assistant/coexist.py check` finds a collision, and `sage doctor` shows it as a FAIL with the
fix. The token window keeps a `CLAUDE_CODE_OAUTH_TOKEN` already stored (every program of the Windows account runs on it)
unless the owner types REPLACE, and an API key (which bills every Claude run on the account per token) only after METERED.
`bootstrap.ps1` / `.sh` never re-use a folder that holds another organization's agent.

## Updates: one command, three installs

```bash
python assistant/sage.py update            # the release feed (org.json engine_release_url, else the engine clone's GitHub Releases), gated, pack files never touched
python assistant/sage.py update --check    # what the feed offers
python assistant/sage.py update --engine <sage-core folder>   # from a clone, offline
python assistant/sage.py rollback          # the previous engine back in one step
```

The supervisor runs the feed path once a night (`serve.json` job `engine-update`, 03:00-05:00): `assistant/updater.py` reads `release.json`, compares the version (the date part; a different build of the same date is applied only with `--force`), downloads the zip, checks its sha256, extracts it to `<pack>/.sage-engines/staging-<version>/`, gates it there (every `.py` compiles, `sage.py doctor --no-probe` runs against this pack's `org.json` from the staged copy, the staged engine's `assistant/tests/test_sage_core.py` passes), snapshots the pack's current engine files to `.sage-engines/previous/`, applies with `engine.update` (hand-edited engine files are reported and left alone; pack files are never touched), keeps the staged copy as `.sage-engines/<version>/`, and asks the supervisor to re-exec on the new code (`assistant/data/.sage_restart`). Any failure leaves the pack as it was and posts one note on the agent's page (never Chat). Packs made by the installer and packs made from a clone take this same path.

## Releases

```bash
python assistant/release.py build [--out dist] [--url <base>] [--notes "..."]   # dist/sage-core-<version>.zip + release.json (+ .sha256)
python assistant/release.py verify dist/sage-core-<version>.zip
```

Run from an engine checkout (sage-core) the zip is the checkout's manifest files as they are; run from an organization's folder the engine is exported first (`engine.export`: root → `{{ORG_ROOT}}`, every name scrubbed). Versions are `YYYY.MM.DD+<git short sha>`. Publishing a release = putting `release.json`, the zip and its `.sha256` where `-ReleaseUrl` points.
