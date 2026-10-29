# spine_long.ps1 - Task Scheduler entry for the Day Spine's LONG jobs (every 10 min, its own 25-min limit):
# the doer tick and the executor drain (spine_jobs.json "group": "long"). Kept apart from spine.ps1 so a slow
# browser run never starves the card sweep, the sentinels or the lead sensor (WP2, 2026-10-24). Zero LLM.
$ErrorActionPreference = 'Continue'
$vault = "{{ORG_ROOT}}"
$py    = "C:\Users\owner1\.venvs\wedding-web\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Set-Location $vault
& $py "$vault\assistant\scripts\spine.py" --group long 2>&1 | Out-Null
exit 0
