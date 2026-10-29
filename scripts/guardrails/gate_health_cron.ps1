# gate_health_cron.ps1 - run the send-gate tripwire on a schedule; Mission Control alert on regression.
#
# Zero-LLM, stdlib-only. Catches RUNTIME gate regressions the pre-commit hook can't (e.g. the
# 6:30 AM dreaming auto-apply rewriting .claude/settings.json). On pass it is silent; on failure
# gate_health_check.py posts a Mission Control card and this logs the detail.
# Telegram was removed 2026-10-04.
#
# REGISTER FROM THE TERMINAL SESSION (Cowork can't register tasks - Access Denied):
#   schtasks /create /xml "{{ORG_ROOT}}\scripts\guardrails\gate_health.xml" /tn "Wedding Gate Health"
# Logs (failures only) -> ~/.claude/gate-health-wv.log
$ErrorActionPreference = 'Continue'
$vault  = "{{ORG_ROOT}}"
$py     = Join-Path $env:USERPROFILE ".venvs\vault-mem\Scripts\python.exe"
$script = Join-Path $vault "scripts\guardrails\gate_health_check.py"
$log    = Join-Path $env:USERPROFILE ".claude\gate-health-wv.log"
if (-not (Test-Path $py)) { $py = "python" }
$out = & $py $script --quiet 2>&1
if ($LASTEXITCODE -ne 0) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  GATE REGRESSION (exit $LASTEXITCODE):`n$out" | Add-Content -Path $log
}
