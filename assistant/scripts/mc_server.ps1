# mc_server.ps1 - keepalive launcher for the personal-OS Mission Control server.
# Called every 10 min by the Day Spine (job mc-keepalive). Exits at once if 127.0.0.1:9019
# already answers; otherwise starts the server DETACHED and returns, so the caller never
# blocks on (or kills) the long-running server process.
#   -Restart : stop whatever listens on :9019 first (use after editing mc_server.py / dashboard.py)
param([switch]$Restart)
$ErrorActionPreference = 'Continue'
$vault = "{{ORG_ROOT}}"
$py    = "C:\Users\owner1\.venvs\wedding-web\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { $py = "C:\Users\owner1\.venvs\wedding-web\Scripts\python.exe" }
if (-not (Test-Path $py)) { $py = "python" }
# a new organization's PC has its own interpreter: ASSISTANT_PY, then org.json -> python, win over the paths above (2026-11-15:
# an installed organization's operator, spine and deadman all called a venv that only exists on the source PC)
try { $orgPy = [string]((Get-Content (Join-Path $vault 'assistant\org.json') -Raw -ErrorAction Stop | ConvertFrom-Json).python) } catch { $orgPy = '' }
if ($orgPy) { $orgPy = [Environment]::ExpandEnvironmentVariables($orgPy) -replace '^~', $env:USERPROFILE }
if ($env:ASSISTANT_PY -and (Test-Path $env:ASSISTANT_PY)) { $py = $env:ASSISTANT_PY } elseif ($orgPy -and (Test-Path $orgPy)) { $py = $orgPy }
$pyw = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
if ((Split-Path -Leaf $py) -eq 'python.exe' -and (Test-Path $pyw)) { $py = $pyw }   # no console window for the page server
# the port guard (assistant/ports.py): Mission Control may have moved off :9019 when another program held it
$pyc = $py -replace 'pythonw\.exe$', 'python.exe'
$port = 9019
try { $p = & $pyc "$vault\assistant\ports.py" current mission_control 2>$null; if ("$p" -match '^\d+$') { $port = [int]$p } } catch {}
if ($Restart) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
} else {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { exit 0 }
    } catch {}
}
Start-Process -FilePath $py -ArgumentList "`"$vault\assistant\scripts\mc_server.py`"" -WorkingDirectory $vault -WindowStyle Hidden
exit 0
