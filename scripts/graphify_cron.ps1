# graphify_cron.ps1 - scheduled staleness watch for this folder's knowledge graph.
#
# WHY: docs under Wiki/Operations are often edited directly in Obsidian without an
# immediate git commit, so the post-commit hook alone can miss drift. This cron is the
# time-based safety net: a few times a day it checks staleness and, when the graph has
# drifted, REBUILDS it automatically.
#
# The wedding folder graph is a wikilink reference graph, so it rebuilds fully
# deterministically (scripts/graphify_rebuild.py) with zero LLM calls -- no Claude
# subagents (which fail on this machine's OpenRouter routing) and no Gemini key needed.
# That makes genuine unattended auto-rebuild possible. On the rare chance the rebuild
# errors, it notifies the owner via Mission Control so the drift is never silently lost.
#
# Registration (must run from the TERMINAL CLI session, not desktop/Cowork -- see global
# CLAUDE.md "Scheduled-task registration"):
#   schtasks /create /xml "{{ORG_ROOT}}\scripts\graphify_cron.xml" /tn "Wedding Graphify (Staleness watch)"
#
# Regression self-test (run after any change to graphify_rebuild.py or this cron):
#   python scripts/graphify_rebuild.py --self-test
# Inflates graph.json with dummy nodes, runs a real rebuild, asserts force=True
# bypassed the shrink-guard and the result has real edges.  Exit 0 = pass, exit 1 = FAIL.
#
# Logs to ~/.claude/graphify-wv.log.

$ErrorActionPreference = 'Stop'
$root  = Split-Path -Parent $PSScriptRoot          # this script lives in <root>\scripts
$out   = Join-Path $root 'graphify-out'
$log   = Join-Path $env:USERPROFILE '.claude\graphify-wv.log'
$state = Join-Path $out '.graphify_cron_state.json'

function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $log -Value $line -Encoding utf8
}

function Get-OrgPython {
    # The interpreter org.json names (semantic_python, then python; ~ expands to the user's home), else plain python
    # on PATH (2026-10-29: the venv path used to be this PC's).
    try {
        $o = Get-Content (Join-Path $root 'assistant\org.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        foreach ($k in 'semantic_python', 'python') {
            $v = $o.$k
            if ($v) {
                $v = [Environment]::ExpandEnvironmentVariables([string]$v) -replace '^~', $env:USERPROFILE
                if (Test-Path $v) { return $v }
            }
        }
    } catch {}
    return 'python'
}
$py = Get-OrgPython

function Notify-MissionControl($text) {
    # Route to Mission Control (assistant/data/cards.jsonl). Never throws.
    # Telegram was removed 2026-10-04; Mission Control is the only channel.
    try {
        $tmp = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($tmp, [string]$text, (New-Object System.Text.UTF8Encoding($false)))
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            $r = (& $py (Join-Path $root 'assistant\scripts\notify.py') '--file' $tmp 2>&1 | ForEach-Object { "$_" }) -join ' '
        } finally { $ErrorActionPreference = $old }
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Log "MISSION-CONTROL: $r"
    } catch { Log "mission-control notify failed -- $($_.Exception.Message)" }
}

try {
    Log '--- run start ---'

    # 1. Deterministic staleness check (writes/clears graphify-out/.needs_update).
    $json = & $py (Join-Path $root 'scripts\graphify_stale.py') --json 2>&1 | Select-Object -Last 1
    $r = $null
    try { $r = $json | ConvertFrom-Json } catch { Log "stale-check parse error: $json"; Log '--- run end (error) ---'; exit 1 }

    if ($r.state -eq 'no-graph') { Log 'no graph built yet -- nothing to watch'; Log '--- run end ---'; exit 0 }
    if (-not $r.stale) { Log 'graph is fresh'; Log '--- run end ---'; exit 0 }

    $nNew = @($r.new).Count; $nDel = @($r.deleted).Count; $nMod = @($r.modified).Count
    # Sig: sorted filenames (not just counts) so two DIFFERENT sets of 3 modified files
    # don't share the same sig and silently skip a second Mission Control notification.
    $allFiles = (@($r.new) + @($r.deleted) + @($r.modified)) | Sort-Object
    $sigRaw   = $allFiles -join '|'
    $sha      = [System.Security.Cryptography.SHA1]::Create()
    $sigHash  = [Convert]::ToBase64String($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($sigRaw))).Substring(0,8)
    $sig = "${nNew}/${nDel}/${nMod}:${sigHash}"
    Log "STALE: $nNew new, $nDel deleted, $nMod modified"

    # 2. Deterministic, LLM-free auto-rebuild (the wikilink graph needs no model).
    # NOTE: With $ErrorActionPreference='Stop', `2>&1` on a native exe wraps
    # stderr lines as ErrorRecords and any of them (even a graphify WARNING) can
    # throw to the catch block before $LASTEXITCODE is ever checked.  Fix: use a
    # nested scope with Continue preference so stderr output is captured/logged
    # without throwing, then check $LASTEXITCODE normally.
    $rebuilt = $false
    try {
        $env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
        $rb = & {
            $ErrorActionPreference = 'Continue'
            & $py (Join-Path $root 'scripts\graphify_rebuild.py') 'Wiki/Operations' 2>&1
        }
        $rb | ForEach-Object { Log "  rebuild: $_" }
        if ($LASTEXITCODE -eq 0) { $rebuilt = $true } else { Log "  rebuild: exit $LASTEXITCODE" }
    } catch {
        Log "rebuild threw -- $($_.Exception.Message)"
    }

    if ($rebuilt) {
        # Confirm fresh + clear the flag, then notify success (deduped per change-set).
        & $py (Join-Path $root 'scripts\graphify_stale.py') --quiet 2>&1 | Out-Null
        $last = ''
        if (Test-Path $state) { try { $last = (Get-Content $state -Raw | ConvertFrom-Json).sig } catch {} }
        if ($sig -ne $last) {
            Notify-MissionControl "Graphify: graph auto-rebuilt ($nNew new, $nMod modified doc(s) in Wiki/Operations)."
            @{ sig = $sig; at = (Get-Date -Format 'o') } | ConvertTo-Json | Set-Content -Path $state -Encoding utf8
        }
        Log '--- run end (rebuilt) ---'
        exit 0
    }

    # 3. Rebuild failed: notify once per distinct change-set so the drift isn't lost.
    $last = ''
    if (Test-Path $state) { try { $last = (Get-Content $state -Raw | ConvertFrom-Json).sig } catch {} }
    if ($sig -ne $last) {
        Notify-MissionControl "Graphify: graph is STALE ($nNew new, $nMod modified) and auto-rebuild failed. Check ~/.claude/graphify-wv.log."
        @{ sig = $sig; at = (Get-Date -Format 'o') } | ConvertTo-Json | Set-Content -Path $state -Encoding utf8
    } else {
        Log 'change-set unchanged since last notify -- not re-pinging'
    }
    Log '--- run end (rebuild failed) ---'
    exit 1
}
catch {
    Log "FATAL: $($_.Exception.Message)"
    exit 1
}
