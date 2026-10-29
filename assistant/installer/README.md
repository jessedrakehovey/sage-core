# Installing a Sage agent (sage-core)

Three ways onto a machine; every one of them is kept current by the same update path.

## 1. The installer (Windows, no terminal, no git)

`SageSetup.exe` installs per user, no admin, under `%LOCALAPPDATA%\Sage`:

- `python\`: the python.org embeddable Python 3.11 with pip and `assistant/requirements.txt` (private to Sage; the system's Python is never touched)
- `engine\`: the engine release (`engine_manifest.json`, `assistant/`, `scripts/`, `Schema/`, `.claude/`)
- the Playwright Chromium browser (`%LOCALAPPDATA%\ms-playwright`) and the Claude Code CLI by its native installer (`%USERPROFILE%\.local\bin\claude.exe`; skipped when a `claude` is already on the machine)
- a Start-menu entry **Sage** that opens the agent's page when the organization runs, starts it when it does not, and the setup page before any organization exists
- `install.json`: where these are, read by the setup page

Then the browser opens the **setup page** (`assistant/welcome.py`, `http://127.0.0.1:9018/welcome`): the organization's name, what to call the agent, the owners (name + email), the folder (default `Documents\<Org> OS`); it creates the pack with `new_org.py`, shows the doctor table with a fix line per FAIL, opens the one window the owner types in themselves (the Claude token: a console that runs `claude setup-token` and stores the pasted result as the User environment variable `CLAUDE_CODE_OAUTH_TOKEN`; nothing secret passes through the page), lists the Google clicks with one link each, and starts the supervisor (`sage.py install-startup`: at every logon from then on). The agent's page serves the same wizard at `/welcome` afterwards; the page carries the organization's name and the agent's name (an organization may set its own title in profile.json `page_title`).

Uninstall (Settings → Apps → Sage) stops the supervisor, removes the logon entry, `python\`, `engine\` and `bootstrap\`. **The organization's folder is never deleted**; `sage.py uninstall` prints what else is the owner's to revoke (the token, the Google clients).

Build it (needs Inno Setup 6, a user install: `winget install JRSoftware.InnoSetup --scope user`):

```powershell
powershell -ExecutionPolicy Bypass -File assistant\installer\windows\build.ps1 -ReleaseUrl https://<host>/<path>
```

`build.ps1` runs `assistant/release.py build` (the zip + `release.json` into `dist/`) and `ISCC` on `SageSetup.iss`; the result is `dist\SageSetup.exe`. `-ReleaseUrl` is where the release files will be served from and is written into the installer so every organization it installs knows where to look for updates; it defaults to the checkout's GitHub origin as `.../releases/latest/download` (the engine itself carries no account name). Publishing is one command from the engine checkout, with the GitHub CLI signed in:

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
