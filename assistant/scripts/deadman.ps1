# deadman.ps1 - Task Scheduler entry for the deadman watcher (every 30 min). Zero LLM. Imports nothing from
# assistant/ (deadman.py is stdlib only): the thing that watches the watchers must not depend on them.
$ErrorActionPreference = 'Continue'
$vault = "{{ORG_ROOT}}"
$py    = "C:\Users\owner1\.venvs\wedding-web\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Set-Location $vault
& $py "$vault\assistant\scripts\deadman.py" 2>&1 | Out-Null
exit 0
