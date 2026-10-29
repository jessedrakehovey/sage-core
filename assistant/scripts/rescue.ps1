# rescue.ps1 - local rescue kit for the personal OS (roadmap Phase 5, lean port of the vault's rescue/backup.ps1).
# Mirrors the git repo (full history) + the git-ignored runtime state that matters (assistant/data,
# dashboard/data, scripts/projects) onto other physical drives. No cloud, no push: a GitHub mirror is
# the owner's call (see ROADMAP decisions). Idempotent; safe to run from the Day Spine daily.
#   powershell -File assistant/scripts/rescue.ps1 [-Targets I:\VAULT_RESCUE,L:\VAULT_RESCUE]
param([string[]]$Targets = @("I:\VAULT_RESCUE", "L:\VAULT_RESCUE"))
$ErrorActionPreference = 'Continue'
$vault = "{{ORG_ROOT}}"
$log = "$env:USERPROFILE\.claude\rescue-wv.log"
function Log($m) { $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $m; Write-Output $line; Add-Content -Path $log -Value $line -ErrorAction SilentlyContinue }
$done = 0
foreach ($t in $Targets) {
    $drive = Split-Path $t -Qualifier
    if (-not (Test-Path "$drive\")) { Log "skip $t (drive not present)"; continue }
    try {
        New-Item -ItemType Directory -Force "$t" | Out-Null
        $mirror = Join-Path $t "WeddingVideo.git"
        if (Test-Path $mirror) {
            & git -C $vault push --mirror $mirror 2>&1 | Out-Null
        } else {
            & git clone --mirror $vault $mirror 2>&1 | Out-Null
        }
        $state = Join-Path $t "WeddingVideo.state"
        foreach ($sub in @("assistant\data", "dashboard\data", "scripts\projects", "scripts\lib\.cache")) {
            $src = Join-Path $vault $sub
            if (Test-Path $src) { robocopy $src (Join-Path $state $sub) /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null }
        }
        $head = (& git -C $vault rev-parse --short HEAD)
        Set-Content -Path (Join-Path $t "WeddingVideo.RESTORE.txt") -Encoding utf8 -Value @"
example-org / personal OS rescue kit  (written $(Get-Date -Format s), HEAD $head)
Restore on a new PC:
  git clone "$mirror" "{{ORG_ROOT}}"
  copy WeddingVideo.state\* back into the clone (assistant\data, dashboard\data, scripts\projects, scripts\lib\.cache)
  set secrets again: python assistant\scripts\setup_creds.py mail personal / mail example / calendar personal
  register tasks: schtasks /create /xml for each XML in assistant\tasks, ads\tasks, scripts\*.xml (UTF-16)
  tailscale serve --bg --https=9443 http://127.0.0.1:9019
"@
        Log "ok $t (HEAD $head)"
        $done++
    } catch { Log "FAIL $t : $($_.Exception.Message)" }
}
# Off-machine copy: push to the private GitHub mirror when the owner has added it as `origin`
# (he creates it himself: gh repo create owner/wedding-video-os --private --source . --remote origin --push).
$origin = (& git -C $vault remote get-url origin 2>$null)
if ($origin) {
    & git -C $vault push origin --all --quiet 2>&1 | Out-Null
    & git -C $vault push origin --tags --quiet 2>&1 | Out-Null
    Log "github mirror pushed ($origin)"
} else { Log "github mirror: no origin remote yet (owner1's step)" }
Log "RESCUE targets_ok=$done"
