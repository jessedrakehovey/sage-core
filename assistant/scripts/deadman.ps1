# deadman.ps1 - Task Scheduler entry for the deadman watcher (every 30 min). Zero LLM. Imports nothing from
# assistant/ (deadman.py is stdlib only): the thing that watches the watchers must not depend on them.
$ErrorActionPreference = 'Continue'
$vault = "{{ORG_ROOT}}"
$py    = "C:\Users\owner1\.venvs\wedding-web\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
# a new organization's PC has its own interpreter: ASSISTANT_PY, then org.json -> python, win over the paths above (2026-11-15:
# an installed organization's operator, spine and deadman all called a venv that only exists on the source PC)
try { $orgPy = [string]((Get-Content (Join-Path $vault 'assistant\org.json') -Raw -ErrorAction Stop | ConvertFrom-Json).python) } catch { $orgPy = '' }
if ($orgPy) { $orgPy = [Environment]::ExpandEnvironmentVariables($orgPy) -replace '^~', $env:USERPROFILE }
if ($env:ASSISTANT_PY -and (Test-Path $env:ASSISTANT_PY)) { $py = $env:ASSISTANT_PY } elseif ($orgPy -and (Test-Path $orgPy)) { $py = $orgPy }
Set-Location $vault
& $py "$vault\assistant\scripts\deadman.py" 2>&1 | Out-Null
exit 0
