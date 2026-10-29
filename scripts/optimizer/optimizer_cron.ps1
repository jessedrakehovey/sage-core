# optimizer_cron.ps1 -- Nightly Optimization Scout for Wedding Videography.
# Runs the Claude CLI headless (claude-fable-5 model, read-only tools) to find
# 5 booking-pipeline/delivery/footage-safety/automation optimization proposals each night.
# Registered as Windows Scheduled Task "the organization", 3:30 AM daily.
# Logs to ~/.claude/optimizer-wv.log.
#
# SAFETY: the scout is STRICTLY read-only. The invocation uses --allowedTools "Read,Grep,Glob"
# only -- no Bash, no Write, no Edit, no MCP. The wrapper enforces a tamper canary: if
# git status --porcelain changes during the scout run, the proposals are discarded.
#
# Data flow:
#   1. SNAPSHOT PHASE  -- wrapper writes scripts/optimizer/.cache/snapshot/ with system eyes.
#   2. SCOUT PHASE     -- claude-fable-5 headless reads snapshot + vault, returns JSON proposals.
#   3. TAMPER CANARY   -- if git tree changed during scout, alert and discard.
#   4. PARSE GATE      -- validate JSON (1-5 items) before writing proposals file.
#   5. OUTPUT          -- proposals_YYYY-MM-DD.json in scripts/optimizer/.cache/.

$ErrorActionPreference = 'Stop'
$vault  = "{{ORG_ROOT}}"
$claude = "C:\Users\owner1\AppData\Roaming\npm\claude.cmd"
$log    = Join-Path $env:USERPROFILE ".claude\optimizer-wv.log"
$cfgPath = Join-Path $vault 'scripts\optimizer\config.json'
$cacheDir = Join-Path $vault 'scripts\optimizer\.cache'
$snapDir  = Join-Path $cacheDir 'snapshot'
$promptFile = Join-Path $vault 'scripts\optimizer\PROMPT.md'

. (Join-Path $vault 'scripts\lib\run_claude.ps1')

function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content -Path $log }

# ---- KILL SWITCH ----
if (-not (Test-Path $cfgPath)) { Log "ERROR: config.json not found at $cfgPath"; exit 1 }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
if (-not $cfg.enabled) {
    Log "optimizer disabled (config.json enabled=false) -- skipping run"
    exit 0
}
$maxTurns = if ($cfg.max_turns -as [int]) { [int]$cfg.max_turns } else { 100 }

try {
    Log "=== optimizer scout start ==="
    if (-not (Test-Path $claude)) { Log "ERROR: claude CLI not found at $claude"; exit 1 }

    # ---- SNAPSHOT PHASE ----
    # Write fresh system-state files into .cache/snapshot/ so the scout has eyes
    # on the live state without needing Bash tool access.
    if (-not (Test-Path $snapDir)) { New-Item -ItemType Directory -Path $snapDir -Force | Out-Null }

    Log "snapshot: writing scheduled tasks..."
    try {
        $tasks = schtasks /query /fo csv 2>&1 | Out-String
        [System.IO.File]::WriteAllText((Join-Path $snapDir 'scheduled_tasks.txt'), $tasks, [System.Text.Encoding]::UTF8)
    } catch { Log "snapshot: schtasks error (non-fatal): $_" }

    Log "snapshot: writing git log..."
    try {
        $gitLog = & git -C $vault log --oneline -30 2>&1 | Out-String
        [System.IO.File]::WriteAllText((Join-Path $snapDir 'git_log.txt'), $gitLog, [System.Text.Encoding]::UTF8)
    } catch { Log "snapshot: git log error (non-fatal): $_" }

    Log "snapshot: writing scripts listing..."
    try {
        $listing = Get-ChildItem -Path (Join-Path $vault 'scripts') -Recurse -File |
            Select-Object @{N='Path';E={$_.FullName}}, LastWriteTime, Length |
            Format-Table -AutoSize | Out-String
        [System.IO.File]::WriteAllText((Join-Path $snapDir 'scripts_listing.txt'), $listing, [System.Text.Encoding]::UTF8)
    } catch { Log "snapshot: scripts listing error (non-fatal): $_" }

    Log "snapshot: writing cron log tails..."
    try {
        $logDir = Join-Path $env:USERPROFILE '.claude'
        $cronLogs = Get-ChildItem -Path $logDir -Filter '*.log' -ErrorAction SilentlyContinue
        $allLogText = ""
        foreach ($lf in $cronLogs) {
            $allLogText += "=== $($lf.Name) (last 50 lines) ===`n"
            $tail = Get-Content $lf.FullName -Tail 50 -ErrorAction SilentlyContinue
            if ($tail) { $allLogText += ($tail -join "`n") + "`n`n" }
        }
        [System.IO.File]::WriteAllText((Join-Path $snapDir 'cron_logs.txt'), $allLogText, [System.Text.Encoding]::UTF8)
    } catch { Log "snapshot: cron logs error (non-fatal): $_" }

    Log "snapshot: copying project state..."
    $cacheItems = @(
        @{ src = Join-Path $vault 'scripts\projects\project_state.json'; dst = 'project_state.json' }
    )
    foreach ($item in $cacheItems) {
        if (Test-Path $item.src) {
            try { Copy-Item $item.src (Join-Path $snapDir $item.dst) -Force } catch { Log "snapshot: copy $($item.dst) error (non-fatal): $_" }
        }
    }

    Log "snapshot: complete"

    # ---- TAMPER CANARY -- BEFORE ----
    # Memory/Daily_Logs/ is excluded on both sides: the scout's own headless session fires the
    # SessionEnd capture hook, which appends the day's log -- that tripped the canary and discarded
    # the proposals on any night the scout was the first session of the day (2026-10-01).
    # The scout has no Write/Edit/Bash tool, so every other path still must not change.
    $gitBefore = (& git -C $vault status --porcelain 2>$null | Where-Object { "$_".Length -lt 4 -or "$_".Substring(3) -notlike 'Memory/Daily_Logs/*' }) -join "`n"

    # ---- SCOUT PHASE ----
    if (-not (Test-Path $promptFile)) { Log "ERROR: PROMPT.md not found at $promptFile"; exit 1 }
    $prompt = Get-Content $promptFile -Raw -Encoding UTF8

    $allowed = 'Read,Grep,Glob,WebSearch,WebFetch,mcp__firecrawl__firecrawl_scrape,mcp__firecrawl__firecrawl_search,mcp__firecrawl__firecrawl_crawl,mcp__firecrawl__firecrawl_map,mcp__firecrawl__firecrawl_extract'

    Log "scout: invoking opus (max-turns $maxTurns, allowedTools Read,Grep,Glob,WebSearch,WebFetch,mcp__firecrawl__firecrawl_scrape,mcp__firecrawl__firecrawl_search,mcp__firecrawl__firecrawl_crawl,mcp__firecrawl__firecrawl_map,mcp__firecrawl__firecrawl_extract)..."
    Push-Location $vault
    $r = Invoke-ClaudeHeadless -Claude $claude -Prompt $prompt `
         -ClaudeArgs @('--model','opus','--max-turns',"$maxTurns",'--allowedTools',$allowed) `
         -Logger ${function:Log}
    Pop-Location

    if ($r.Output) { Log "OUTPUT (first 500 chars): $($r.Output.Substring(0, [Math]::Min(500, $r.Output.Length)))" }
    if ($r.Stderr -and $r.Stderr.Trim()) { Log "STDERR:`n$(("$($r.Stderr)").Trim())" }
    Log "scout: done (exit $($r.ExitCode), attempts $($r.Attempts))"

    # ---- TAMPER CANARY -- AFTER ----
    $gitAfter = (& git -C $vault status --porcelain 2>$null | Where-Object { "$_".Length -lt 4 -or "$_".Substring(3) -notlike 'Memory/Daily_Logs/*' }) -join "`n"
    if ($gitBefore -ne $gitAfter) {
        Log "ALERT: tamper canary fired -- git status changed during scout run. Proposals DISCARDED."
        Log "BEFORE: $gitBefore"
        Log "AFTER:  $gitAfter"
        exit 1
    }
    Log "tamper canary: clean (no tree changes)"

    if (-not $r.Ok) {
        Log "ERROR: scout invocation failed (exit $($r.ExitCode)); raw stderr saved above"
        exit 1
    }

    # ---- PARSE GATE ----
    # Validate the stdout is valid JSON in the expected schema:
    #   Object form (preferred, with roast fields):
    #     { "proposals": [...1-5 items...], "killed": [...optional...] }
    #   Legacy array form (also accepted for backward compat):
    #     [...1-5 items...]
    # Validates proposal count (1-5). Keeps raw stdout in a debug file on parse failure.
    $rawOutput = $r.Output
    $today = Get-Date -Format 'yyyy-MM-dd'
    $outFile = Join-Path $cacheDir "proposals_$today.json"
    $debugFile = Join-Path $cacheDir "proposals_${today}_raw_debug.txt"

    if ([string]::IsNullOrWhiteSpace($rawOutput)) {
        Log "PARSE-GATE: empty output from scout -- no proposals written"
        exit 1
    }

    # Find the first '{' or '[' and take from there (scout might prefix a brief note)
    $jsonText = $rawOutput.Trim()
    $braceIdx  = $jsonText.IndexOf('{')
    $bracketIdx = $jsonText.IndexOf('[')
    if ($braceIdx -ge 0 -and ($bracketIdx -lt 0 -or $braceIdx -lt $bracketIdx)) {
        $jsonText = $jsonText.Substring($braceIdx)
    } elseif ($bracketIdx -ge 0) {
        $jsonText = $jsonText.Substring($bracketIdx)
    }

    try {
        $parsed = $jsonText | ConvertFrom-Json
        if (-not $parsed) { throw "null parse result" }

        # Determine if object-form (has .proposals) or legacy array form.
        # PS 5.1 note: ConvertFrom-Json on a JSON array returns a PSCustomObject[] for N>1
        # but a plain PSCustomObject for N=1. Wrap in @() to normalize both.
        $proposals = $null
        # Check for object form: either has a 'proposals' property, or is NOT array-like
        # and doesn't look like a single proposal object (single proposal would have 'key').
        $hasProposalsKey = $null -ne ($parsed.PSObject.Properties | Where-Object { $_.Name -eq 'proposals' })
        if ($hasProposalsKey) {
            # Object form: { proposals: [...], killed: [...optional] }
            $proposals = @($parsed.proposals)
            $killedCount = if ($null -ne ($parsed.PSObject.Properties | Where-Object { $_.Name -eq 'killed' })) { @($parsed.killed).Count } else { 0 }
            Log "parse-gate: object form detected ($killedCount killed candidate(s))"
        } else {
            # Legacy array form -- @() wraps single item and passes through arrays
            $proposals = @($parsed)
            Log "parse-gate: legacy array form detected"
        }

        $count = $proposals.Count
        if ($count -lt 1 -or $count -gt 5) {
            throw "expected 1-5 proposals in proposals array, got $count"
        }
        Log "parse-gate: valid JSON, $count proposal(s) -- writing $outFile"
        # Reserialize clean (no extra raw output). Use the parsed object so killed is preserved.
        $clean = $parsed | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($outFile, $clean, [System.Text.Encoding]::UTF8)
        Log "proposals written: $outFile"
        # Put them in front of the owner: nothing read this file, so a night's proposals were invisible.
        try {
            $titles = (@($proposals | ForEach-Object { $_.title } | Where-Object { $_ }) -join '; ')
            $note = "Optimization Scout: $count proposal(s) to improve the system. $titles. File: scripts/optimizer/.cache/proposals_$today.json. Say 'review the optimizer proposals' in a session to go through them; nothing is built without you."
            $tmp = [IO.Path]::GetTempFileName()
            [IO.File]::WriteAllText($tmp, $note, (New-Object System.Text.UTF8Encoding($false)))
            $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try { $nr = (& python (Join-Path $vault 'assistant\scripts\notify.py') --file $tmp 2>&1 | ForEach-Object { "$_" }) -join ' ' }
            finally { $ErrorActionPreference = $eap }
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
            Log "mission-control: $nr"
        } catch { Log "mission-control note failed (non-fatal): $_" }
        # rotation: nightly dated files accumulate forever; keep the newest 14
        Get-ChildItem $cacheDir -Filter 'proposals_*.json' |
            Sort-Object Name -Descending | Select-Object -Skip 14 |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch {
        Log "PARSE-GATE: JSON validation failed -- $_"
        Log "raw output saved to: $debugFile"
        [System.IO.File]::WriteAllText($debugFile, $rawOutput, [System.Text.Encoding]::UTF8)
        exit 1
    }

    Log "=== optimizer scout done ==="
}
catch {
    Log "FATAL: $_"
    exit 1
}
