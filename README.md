# sage-core 2026.10.30+aa26830

The engine of a Sage agent: the code, generic prompts, generic skills and templates, with nothing of any
organization in it. An organization is a separate folder (its *pack*) made from this engine by
`python assistant/new_org.py "<Org>" --owner "Name:email" --out <folder>`; the pack is updated to a newer
engine with `python assistant/sage.py update --engine <this folder>` (pack files are never touched; engine
files the organization changed by hand are reported, not overwritten).

Start here: `python assistant/sage.py doctor` (what this machine still needs), then the `sage-setup` skill in
`assistant/setup_skill/` (install it with `python assistant/sage.py install-skill`).

## Installing

Three ways onto a machine, one update path (`assistant/installer/README.md` has the detail):

1. **The installer (Windows, no terminal, no git):** `SageSetup.exe` (built by `assistant/installer/windows/build.ps1`)
   installs a private Python, this engine as a release, the browser and the Claude Code CLI under `%LOCALAPPDATA%\Sage`,
   then opens the setup page (`assistant/welcome.py`): the organization's name, what to call the agent, the owners; it
   creates the pack, shows the doctor table, opens the one window the owner types the Claude token into, lists the
   Google clicks, starts the agent. Nothing secret passes through the page.
2. **A git clone:** `python assistant/new_org.py "<Org>" --agent <Name> --owner "Full Name:email" --out <folder>`,
   then `python assistant/sage.py doctor` and `serve` in that folder (`sage welcome` opens the same setup page).
3. **The copilot skill** (`/sage-setup` in Claude Code) for a guided install; it ends in the same pack.

Update, whichever way it was installed: `python assistant/sage.py update` (the release feed, `org.json ->
engine_release_url`, gated in a staging folder, pack files never touched; the supervisor runs it nightly),
`sage update --engine <this folder>` from a clone, `sage rollback` for the previous engine. Releases:
`python assistant/release.py build` (the zip + `release.json` into `dist/`).

Optional modules (their jobs start off; `sage module on <name>`):
- `money`: personal-finance lane: read-only bank feeds (Plaid / SimpleFIN), categorising, forecasts; never pays
- `typeform`: Typeform lead sensor

Files: 376. Manifest: `engine_manifest.json`.
