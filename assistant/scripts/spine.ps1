# spine.ps1 - Task Scheduler entry for the Day Spine (every 10 min). Zero LLM.
$ErrorActionPreference = 'Continue'
$vault = "{{ORG_ROOT}}"
$py    = "C:\Users\owner1\.venvs\wedding-web\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Set-Location $vault
& $py "$vault\assistant\scripts\spine.py" 2>&1 | Out-Null
exit 0
