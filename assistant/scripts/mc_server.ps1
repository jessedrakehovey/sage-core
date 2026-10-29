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
if ($Restart) {
    Get-NetTCPConnection -LocalPort 9019 -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
} else {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:9019/api/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { exit 0 }
    } catch {}
}
Start-Process -FilePath $py -ArgumentList "`"$vault\assistant\scripts\mc_server.py`"" -WorkingDirectory $vault -WindowStyle Hidden
exit 0
